from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from src.db.ingestion import (
    persist_feature_set,
    persist_linked_records,
    persist_model_version,
    persist_prediction,
    persist_prediction_batch,
    persist_result_observation,
)
from src.db.models import FeatureSet, Match, ModelVersion, Prediction, ResultObservation, SourceRecord
from scripts.persist_linked_matches import load_linked_records, load_verified_event_times


class FakeSession:
    def __init__(self):
        self.added = []
        self.flushed = False
        self.lookup = {}

    def get(self, model, identity):
        existing = self.lookup.get((model, identity))
        if existing is not None:
            return existing
        for entity in reversed(self.added):
            if not isinstance(entity, model):
                continue
            entity_identity = (
                (entity.model_id, entity.model_version)
                if isinstance(entity, ModelVersion)
                else getattr(entity, "match_id", None)
                if isinstance(entity, Match)
                else getattr(entity, "source_record_id", None)
                if isinstance(entity, SourceRecord)
                else getattr(entity, "feature_set_id", None)
                if isinstance(entity, FeatureSet)
                else getattr(entity, "prediction_id", None)
                if isinstance(entity, Prediction)
                else getattr(entity, "result_observation_id", None)
            )
            if entity_identity == identity:
                return entity
        return None

    def scalar(self, statement):
        return None

    def scalars(self, statement):
        observations = [
            value
            for value in [*self.lookup.values(), *self.added]
            if isinstance(value, ResultObservation)
        ]
        return observations

    def add(self, entity):
        self.added.append(entity)

    def flush(self):
        self.flushed = True


def linked_pair(canonical_id):
    shared = {
        "canonical_match_id": str(canonical_id),
        "match_status": "matched",
        "league": "premier-league",
        "season": "2324",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_goals": 2,
        "away_goals": 1,
        "source_event_ts": "2024-05-12T15:00:00",
        "source_available_ts": "2024-05-13T00:00:00Z",
    }
    return [
        {**shared, "source": "fdco", "source_id": "fdco:1"},
        {**shared, "source": "openfootball", "source_id": "openfootball:1"},
    ]


def test_ingestion_requires_verified_timezone_aware_event_time():
    canonical_id = uuid4()
    session = FakeSession()

    with pytest.raises(ValueError, match="missing verified event time"):
        persist_linked_records(session, linked_pair(canonical_id), {})

    with pytest.raises(ValueError, match="timezone-aware datetime"):
        persist_linked_records(
            session,
            linked_pair(canonical_id),
            {str(canonical_id): datetime(2024, 5, 12, 15, 0)},
        )

    assert session.added == []
    assert not session.flushed


def test_ingestion_requires_two_independent_sources_for_linked_group():
    canonical_id = uuid4()
    records = linked_pair(canonical_id)[:1]
    session = FakeSession()

    with pytest.raises(ValueError, match="two independent sources"):
        persist_linked_records(
            session,
            records,
            {str(canonical_id): datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc)},
        )

    assert session.added == []
    assert not session.flushed


def test_ingestion_rejects_duplicate_provider_identities_before_writing():
    canonical_id = uuid4()
    records = linked_pair(canonical_id)
    records.append(dict(records[0]))
    session = FakeSession()

    with pytest.raises(ValueError, match="duplicate source and source_id identities"):
        persist_linked_records(
            session,
            records,
            {str(canonical_id): datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc)},
        )

    assert session.added == []
    assert not session.flushed


def test_ingestion_persists_provider_updates_as_distinct_revisions():
    canonical_id = uuid4()
    records = linked_pair(canonical_id)
    first_revision = dict(records[0], source_revision=1)
    second_revision = dict(records[0], source_revision=2, home_goals=3)
    records = [first_revision, second_revision, records[1]]
    session = FakeSession()

    result = persist_linked_records(
        session,
        records,
        {str(canonical_id): datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc)},
    )

    source_records = [item for item in session.added if isinstance(item, SourceRecord)]
    observations = [item for item in session.added if isinstance(item, ResultObservation)]
    fdco_records = [item for item in source_records if item.source == "fdco"]
    fdco_observations = [item for item in observations if item.source_record_id in {
        record.source_record_id for record in fdco_records
    }]
    assert result["source_records_inserted"] == 3
    assert [record.source_revision for record in fdco_records] == [1, 2]
    assert len({record.source_record_id for record in fdco_records}) == 2
    assert len(fdco_observations) == 2
    assert len({item.result_observation_id for item in fdco_observations}) == 2


def test_ingestion_rejects_non_positive_source_revision_before_writing():
    canonical_id = uuid4()
    records = linked_pair(canonical_id)
    records[0]["source_revision"] = 0
    session = FakeSession()

    with pytest.raises(ValueError, match="source_revision must be a positive integer"):
        persist_linked_records(
            session,
            records,
            {str(canonical_id): datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc)},
        )

    assert session.added == []
    assert not session.flushed


def test_ingestion_rejects_changed_result_without_new_source_revision():
    canonical_id = uuid4()
    pair = linked_pair(canonical_id)
    record = pair[0]
    source_record_id = uuid5(NAMESPACE_URL, f"{record['source']}:{record['source_id']}:1")
    prior_observation = ResultObservation(
        result_observation_id=uuid4(),
        match_id=canonical_id,
        source_record_id=source_record_id,
        result_data={"home_goals": 2, "away_goals": 1},
        observed_at=datetime(2024, 5, 13, 0, 0, tzinfo=timezone.utc),
        observation_status="observed",
    )
    session = FakeSession()
    session.lookup[(ResultObservation, prior_observation.result_observation_id)] = prior_observation

    with pytest.raises(ValueError, match="provider updates require a new source_revision"):
        persist_linked_records(
            session,
            [dict(record, home_goals=3), pair[1]],
            {str(canonical_id): datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc)},
        )

    assert not any(isinstance(item, ResultObservation) for item in session.added)


def test_ingestion_rejects_changed_provenance_without_new_source_revision(monkeypatch):
    canonical_id = uuid4()
    pair = linked_pair(canonical_id)
    source_record_id = uuid5(NAMESPACE_URL, "fdco:fdco:1")
    existing_record = SourceRecord(
        source_record_id=source_record_id,
        source="fdco",
        source_id="fdco:1",
        source_revision=1,
        match_id=None,
        source_event_ts=None,
        source_available_ts=datetime(2024, 5, 13, 0, 0, tzinfo=timezone.utc),
        ingest_ts=datetime(2024, 5, 14, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        "src.db.ingestion.SourceRecordRepository.find_identity",
        lambda repository, source, source_id, revision: existing_record
        if (source, source_id, revision) == ("fdco", "fdco:1", 1)
        else None,
    )
    session = FakeSession()
    pair[0]["source_available_ts"] = "2024-05-14T00:00:00Z"

    with pytest.raises(ValueError, match="provider updates require a new source_revision"):
        persist_linked_records(
            session,
            pair,
            {str(canonical_id): datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc)},
        )

    assert existing_record.match_id is None
    assert not any(isinstance(item, SourceRecord) for item in session.added)


def test_ingestion_persists_verified_match_and_source_provenance():
    canonical_id = uuid4()
    session = FakeSession()
    result = persist_linked_records(
        session,
        linked_pair(canonical_id),
        {str(canonical_id): datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc)},
    )

    matches = [entity for entity in session.added if isinstance(entity, Match)]
    source_records = [entity for entity in session.added if isinstance(entity, SourceRecord)]
    observations = [entity for entity in session.added if isinstance(entity, ResultObservation)]
    assert result == {
        "matches_inserted": 1,
        "source_records_inserted": 2,
        "source_records_linked": 0,
        "result_observations_inserted": 2,
    }
    assert len(matches) == 1
    assert matches[0].event_time.tzinfo is timezone.utc
    assert len(source_records) == 2
    assert all(record.match_id == canonical_id for record in source_records)
    assert all(record.source_event_ts is None for record in source_records)
    assert all(record.source_available_ts.tzinfo is not None for record in source_records)
    assert len(observations) == 2
    assert all(observation.match_id == canonical_id for observation in observations)
    assert all(
        observation.result_data == {"home_goals": 2, "away_goals": 1}
        for observation in observations
    )
    assert all(
        observation.source_record_id in {record.source_record_id for record in source_records}
        for observation in observations
    )
    assert session.flushed


def test_result_observation_resolution_appends_without_mutating_prior_record():
    session = FakeSession()
    match_id = uuid4()
    prior_observation_id = uuid4()
    result_observation_id = uuid4()
    source_record_id = uuid4()
    observed_at = datetime(2024, 5, 13, 12, 0, tzinfo=timezone.utc)
    prior_observation = ResultObservation(
        result_observation_id=prior_observation_id,
        match_id=match_id,
        result_data={"home_goals": 1, "away_goals": 0},
        observed_at=datetime(2024, 5, 12, 16, 0, tzinfo=timezone.utc),
        observation_status="disputed",
        resolution_reason="Sources disagree",
    )
    session.lookup = {
        (Match, match_id): Match(
            match_id=match_id,
            sport="football",
            league="premier-league",
            season="2324",
            home_team="Arsenal",
            away_team="Chelsea",
            event_time=observed_at,
            status="completed",
        ),
        (SourceRecord, source_record_id): SourceRecord(
            source_record_id=source_record_id,
            source="test-provider",
            source_id="test:1",
            source_revision=1,
            match_id=match_id,
            ingest_ts=observed_at,
        ),
        (ResultObservation, result_observation_id): None,
        (ResultObservation, prior_observation_id): prior_observation,
    }

    resolved = persist_result_observation(
        session,
        result_observation_id=result_observation_id,
        match_id=match_id,
        source_record_id=source_record_id,
        result_data={"home_goals": 2, "away_goals": 0},
        observed_at=observed_at,
        observation_status="resolved",
        supersedes_observation_id=prior_observation_id,
        resolution_reason="Official result correction",
        resolved_at=observed_at,
        resolved_by="reviewer-1",
    )

    assert resolved in session.added
    assert resolved.observation_status == "resolved"
    assert resolved.supersedes_observation_id == prior_observation_id
    assert resolved.source_record_id == source_record_id
    assert prior_observation.observation_status == "disputed"
    assert prior_observation.result_data == {"home_goals": 1, "away_goals": 0}
    assert session.flushed


def test_result_observation_rejects_source_from_a_different_match_before_writing():
    session = FakeSession()
    match_id = uuid4()
    source_record_id = uuid4()
    other_match_id = uuid4()
    session.lookup = {
        (Match, match_id): Match(
            match_id=match_id,
            sport="football",
            league="premier-league",
            season="2324",
            home_team="Arsenal",
            away_team="Chelsea",
            event_time=datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc),
            status="completed",
        ),
        (SourceRecord, source_record_id): SourceRecord(
            source_record_id=source_record_id,
            source="test-provider",
            source_id="test:1",
            source_revision=1,
            match_id=other_match_id,
            ingest_ts=datetime(2024, 5, 12, 16, 0, tzinfo=timezone.utc),
        ),
    }

    with pytest.raises(ValueError, match="same match"):
        persist_result_observation(
            session,
            result_observation_id=uuid4(),
            match_id=match_id,
            source_record_id=source_record_id,
            result_data={"home_goals": 1, "away_goals": 0},
            observed_at=datetime(2024, 5, 12, 16, 0, tzinfo=timezone.utc),
            observation_status="observed",
        )

    assert session.added == []
    assert not session.flushed


def test_result_observation_requires_resolution_metadata():
    session = FakeSession()
    match_id = uuid4()
    session.lookup[(Match, match_id)] = Match(
        match_id=match_id,
        sport="football",
        league="premier-league",
        season="2324",
        home_team="Arsenal",
        away_team="Chelsea",
        event_time=datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc),
        status="completed",
    )

    with pytest.raises(ValueError, match="resolution_reason"):
        persist_result_observation(
            session,
            result_observation_id=uuid4(),
            match_id=match_id,
            result_data={"home_goals": 1, "away_goals": 0},
            observed_at=datetime(2024, 5, 12, 16, 0, tzinfo=timezone.utc),
            observation_status="resolved",
        )

    assert session.added == []
    assert not session.flushed


def test_result_observation_rejects_naive_optional_resolved_at():
    session = FakeSession()
    match_id = uuid4()
    session.lookup[(Match, match_id)] = Match(
        match_id=match_id,
        sport="football",
        league="premier-league",
        season="2324",
        home_team="Arsenal",
        away_team="Chelsea",
        event_time=datetime(2024, 5, 12, 15, 0, tzinfo=timezone.utc),
        status="completed",
    )

    with pytest.raises(ValueError, match="resolved_at must be a timezone-aware datetime"):
        persist_result_observation(
            session,
            result_observation_id=uuid4(),
            match_id=match_id,
            result_data={"home_goals": 1, "away_goals": 0},
            observed_at=datetime(2024, 5, 12, 16, 0, tzinfo=timezone.utc),
            observation_status="observed",
            resolved_at=datetime(2024, 5, 12, 16, 0),
        )

    assert session.added == []
    assert not session.flushed


def test_event_time_csv_requires_an_explicit_timezone(tmp_path):
    event_times_path = tmp_path / "event_times.csv"
    event_times_path.write_text(
        "canonical_match_id,event_time\nmatch-1,2024-05-12T15:00:00+00:00\n",
        encoding="utf-8",
    )

    result = load_verified_event_times(event_times_path)

    assert result["match-1"].utcoffset().total_seconds() == 0

    event_times_path.write_text(
        "canonical_match_id,event_time\nmatch-1,2024-05-12T15:00:00\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must include a timezone offset"):
        load_verified_event_times(event_times_path)


@pytest.mark.parametrize(
    ("availability", "error"),
    [
        (None, "unknown availability"),
        (datetime(2024, 5, 12, 14, 1, tzinfo=timezone.utc), "unavailable at prediction_cutoff"),
    ],
)
def test_feature_set_rejects_unknown_or_late_source_availability(availability, error):
    session = FakeSession()
    match_id = uuid4()
    source_record_id = uuid4()
    cutoff = datetime(2024, 5, 12, 14, 0, tzinfo=timezone.utc)
    session.lookup[(SourceRecord, source_record_id)] = SourceRecord(
        source_record_id=source_record_id,
        source="test-provider",
        source_id="test:1",
        source_revision=1,
        source_available_ts=availability,
        ingest_ts=cutoff,
    )

    with pytest.raises(ValueError, match=error):
        persist_feature_set(
            session,
            feature_set_id=uuid4(),
            match_id=match_id,
            prediction_cutoff=cutoff,
            feature_version="v1",
            feature_generation_version="gen-1",
            source_data_reference={
                "snapshot_id": "snapshot-1",
                "source_record_ids": [str(source_record_id)],
            },
            schema_version="schema-1",
            feature_payload={"home_elo": 1500.0},
        )

    assert session.added == []
    assert not session.flushed


def test_feature_set_allows_source_availability_at_cutoff():
    session = FakeSession()
    match_id = uuid4()
    source_record_id = uuid4()
    cutoff = datetime(2024, 5, 12, 14, 0, tzinfo=timezone.utc)
    session.lookup[(SourceRecord, source_record_id)] = SourceRecord(
        source_record_id=source_record_id,
        source="test-provider",
        source_id="test:1",
        source_revision=1,
        source_available_ts=cutoff,
        ingest_ts=cutoff,
    )

    feature_set = persist_feature_set(
        session,
        feature_set_id=uuid4(),
        match_id=match_id,
        prediction_cutoff=cutoff,
        feature_version="v1",
        feature_generation_version="gen-1",
        source_data_reference={
            "snapshot_id": "snapshot-1",
            "source_record_ids": [str(source_record_id)],
        },
        schema_version="schema-1",
        feature_payload={"home_elo": 1500.0},
    )

    assert feature_set.source_data_reference["source_record_ids"] == [str(source_record_id)]
    assert session.added == [feature_set]
    assert session.flushed


def test_prediction_batch_validates_feature_and_model_contracts():
    session = FakeSession()
    match_id = uuid4()
    feature_set_id = uuid4()
    model_id = uuid4()
    prediction_id = uuid4()
    source_record_id = uuid4()
    cutoff = datetime(2024, 5, 12, 14, 0, tzinfo=timezone.utc)

    session.lookup = {
        (Match, match_id): Match(
            match_id=match_id,
            sport="football",
            league="premier-league",
            season="2324",
            home_team="Arsenal",
            away_team="Chelsea",
            event_time=cutoff,
            status="scheduled",
        ),
        (SourceRecord, source_record_id): SourceRecord(
            source_record_id=source_record_id,
            source="test-provider",
            source_id="test:1",
            source_revision=1,
            source_available_ts=datetime(2024, 5, 12, 13, 0, tzinfo=timezone.utc),
            ingest_ts=datetime(2024, 5, 12, 13, 5, tzinfo=timezone.utc),
        ),
    }

    batch_args = dict(
        match_id=match_id,
        feature_set_id=feature_set_id,
        feature_version="v1.0",
        feature_generation_version="gen-4",
        source_data_reference={"snapshot_id": "snapshot-42", "source_record_ids": [str(source_record_id)]},
        schema_version="schema-1",
        feature_payload={"home_elo": 1500.0},
        model_id=model_id,
        model_version="v3.4.1",
        model_type="xgboost",
        training_run_id=uuid4(),
        training_data_reference={"source": "training-run-72"},
        artifact_reference="s3://models/baseline-v3.json",
        configuration_reference={"objective": "binary:logistic"},
        evaluation_status="passed",
        prediction_id=prediction_id,
        prediction_cutoff=cutoff,
        predicted_at=datetime(2024, 5, 12, 14, 5, tzinfo=timezone.utc),
        prediction="over_2_5",
        probabilities={"over": 0.61, "under": 0.39},
        confidence=0.61,
        status="published",
    )
    feature_set, model_version, prediction = persist_prediction_batch(session, **batch_args)

    assert isinstance(feature_set, FeatureSet)
    assert isinstance(model_version, ModelVersion)
    assert isinstance(prediction, Prediction)
    assert prediction.match_id == match_id
    assert prediction.feature_set_id == feature_set_id
    assert prediction.model_id == model_id
    assert prediction.model_version == "v3.4.1"

    with pytest.raises(ValueError, match="feature_version"):
        persist_prediction_batch(
            session,
            **{**batch_args, "feature_version_model": "v2.0"},
        )

    session.lookup[(ModelVersion, (model_id, "v3.4.1"))] = model_version
    model_rows_before = sum(isinstance(item, ModelVersion) for item in session.added)
    _, reused_model_version, _ = persist_prediction_batch(
        session,
        **{**batch_args, "feature_set_id": uuid4(), "prediction_id": uuid4()},
    )
    assert reused_model_version is model_version
    assert sum(isinstance(item, ModelVersion) for item in session.added) == model_rows_before


def test_direct_prediction_persistence_rejects_cross_match_feature_set():
    session = FakeSession()
    match_id = uuid4()
    other_match_id = uuid4()
    feature_set_id = uuid4()
    model_id = uuid4()
    cutoff = datetime(2024, 5, 12, 14, 0, tzinfo=timezone.utc)
    session.lookup[(FeatureSet, feature_set_id)] = FeatureSet(
        feature_set_id=feature_set_id,
        match_id=other_match_id,
        prediction_cutoff=cutoff,
        feature_version="v1",
        feature_generation_version="gen-1",
        source_data_reference={"snapshot_id": "snapshot-1", "source_record_ids": []},
        schema_version="schema-1",
        feature_payload={},
    )
    session.lookup[(ModelVersion, (model_id, "v1"))] = ModelVersion(
        model_id=model_id,
        model_version="v1",
        model_type="xgboost",
        feature_version="v1",
        training_run_id=uuid4(),
        training_data_reference={},
        artifact_reference="model.json",
        evaluation_status="unevaluated",
    )

    with pytest.raises(ValueError, match="same match as the prediction"):
        persist_prediction(
            session,
            prediction_id=uuid4(),
            match_id=match_id,
            feature_set_id=feature_set_id,
            model_id=model_id,
            model_version="v1",
            feature_version="v1",
            prediction_cutoff=cutoff,
            predicted_at=cutoff,
            prediction="over_2_5",
            probabilities={"over_2_5": 0.6, "under_2_5": 0.4},
            confidence=0.6,
            status="generated",
        )

    assert session.added == []
    assert not session.flushed


def test_model_version_registration_reuses_identical_immutable_revision():
    session = FakeSession()
    model_id = uuid4()
    training_run_id = uuid4()
    model_version = ModelVersion(
        model_id=model_id,
        model_version="v1",
        model_type="xgboost",
        feature_version="features-v1",
        training_run_id=training_run_id,
        training_data_reference={"snapshot": "training-1"},
        artifact_reference="models/v1.json",
        configuration_reference={"objective": "binary:logistic"},
        evaluation_status="unevaluated",
    )
    session.lookup[(ModelVersion, (model_id, "v1"))] = model_version

    result = persist_model_version(
        session,
        model_id=model_id,
        model_version="v1",
        model_type="xgboost",
        feature_version="features-v1",
        training_run_id=training_run_id,
        training_data_reference={"snapshot": "training-1"},
        artifact_reference="models/v1.json",
        configuration_reference={"objective": "binary:logistic"},
        evaluation_status="unevaluated",
    )

    assert result is model_version
    assert session.added == []
    assert not session.flushed


def test_model_version_registration_rejects_conflicting_revision_metadata():
    session = FakeSession()
    model_id = uuid4()
    model_version = ModelVersion(
        model_id=model_id,
        model_version="v1",
        model_type="xgboost",
        feature_version="features-v1",
        training_run_id=uuid4(),
        training_data_reference={"snapshot": "training-1"},
        artifact_reference="models/v1.json",
        configuration_reference={"objective": "binary:logistic"},
        evaluation_status="unevaluated",
    )
    session.lookup[(ModelVersion, (model_id, "v1"))] = model_version

    with pytest.raises(ValueError, match="is immutable"):
        persist_model_version(
            session,
            model_id=model_id,
            model_version="v1",
            model_type="xgboost",
            feature_version="features-v2",
            training_run_id=model_version.training_run_id,
            training_data_reference={"snapshot": "training-1"},
            artifact_reference="models/v1.json",
            configuration_reference={"objective": "binary:logistic"},
            evaluation_status="unevaluated",
        )

    assert session.added == []
    assert not session.flushed


def test_feature_set_identity_is_idempotent_but_immutable():
    session = FakeSession()
    feature_set_id = uuid4()
    match_id = uuid4()
    cutoff = datetime(2024, 5, 12, 14, 0, tzinfo=timezone.utc)
    source_reference = {"snapshot_id": "snapshot-1", "source_record_ids": []}
    feature_set = FeatureSet(
        feature_set_id=feature_set_id,
        match_id=match_id,
        prediction_cutoff=cutoff,
        feature_version="v1",
        feature_generation_version="gen-1",
        source_data_reference=source_reference,
        schema_version="schema-1",
        feature_payload={"home_elo": 1500.0},
    )
    session.lookup[(FeatureSet, feature_set_id)] = feature_set
    values = {
        "feature_set_id": feature_set_id,
        "match_id": match_id,
        "prediction_cutoff": cutoff,
        "feature_version": "v1",
        "feature_generation_version": "gen-1",
        "source_data_reference": source_reference,
        "schema_version": "schema-1",
        "feature_payload": {"home_elo": 1500.0},
    }

    assert persist_feature_set(session, **values) is feature_set
    with pytest.raises(ValueError, match="feature set .* is immutable"):
        persist_feature_set(session, **{**values, "feature_payload": {"home_elo": 1600.0}})
    assert session.added == []
    assert not session.flushed


def test_prediction_identity_is_idempotent_but_immutable():
    session = FakeSession()
    prediction_id = uuid4()
    match_id = uuid4()
    feature_set_id = uuid4()
    model_id = uuid4()
    cutoff = datetime(2024, 5, 12, 14, 0, tzinfo=timezone.utc)
    feature_set = FeatureSet(
        feature_set_id=feature_set_id,
        match_id=match_id,
        prediction_cutoff=cutoff,
        feature_version="v1",
        feature_generation_version="gen-1",
        source_data_reference={"snapshot_id": "snapshot-1", "source_record_ids": []},
        schema_version="schema-1",
        feature_payload={},
    )
    model_version = ModelVersion(
        model_id=model_id,
        model_version="v1",
        model_type="xgboost",
        feature_version="v1",
        training_run_id=uuid4(),
        training_data_reference={},
        artifact_reference="model.json",
        evaluation_status="unevaluated",
    )
    prediction = Prediction(
        prediction_id=prediction_id,
        match_id=match_id,
        feature_set_id=feature_set_id,
        model_id=model_id,
        model_version="v1",
        feature_version="v1",
        prediction_cutoff=cutoff,
        predicted_at=cutoff,
        prediction="over_2_5",
        probabilities={"over_2_5": 0.6, "under_2_5": 0.4},
        confidence=0.6,
        status="generated",
    )
    session.lookup.update({
        (FeatureSet, feature_set_id): feature_set,
        (ModelVersion, (model_id, "v1")): model_version,
        (Prediction, prediction_id): prediction,
    })
    values = {
        "prediction_id": prediction_id,
        "match_id": match_id,
        "feature_set_id": feature_set_id,
        "model_id": model_id,
        "model_version": "v1",
        "feature_version": "v1",
        "prediction_cutoff": cutoff,
        "predicted_at": cutoff,
        "prediction": "over_2_5",
        "probabilities": {"over_2_5": 0.6, "under_2_5": 0.4},
        "confidence": 0.6,
        "status": "generated",
    }

    assert persist_prediction(session, **values) is prediction
    with pytest.raises(ValueError, match="prediction .* is immutable"):
        persist_prediction(session, **{**values, "status": "superseded"})
    assert session.added == []
    assert not session.flushed


def test_direct_prediction_persistence_rejects_model_feature_version_mismatch():
    session = FakeSession()
    match_id = uuid4()
    feature_set_id = uuid4()
    model_id = uuid4()
    cutoff = datetime(2024, 5, 12, 14, 0, tzinfo=timezone.utc)
    session.lookup[(FeatureSet, feature_set_id)] = FeatureSet(
        feature_set_id=feature_set_id,
        match_id=match_id,
        prediction_cutoff=cutoff,
        feature_version="v1",
        feature_generation_version="gen-1",
        source_data_reference={"snapshot_id": "snapshot-1", "source_record_ids": []},
        schema_version="schema-1",
        feature_payload={},
    )
    session.lookup[(ModelVersion, (model_id, "v1"))] = ModelVersion(
        model_id=model_id,
        model_version="v1",
        model_type="xgboost",
        feature_version="v2",
        training_run_id=uuid4(),
        training_data_reference={},
        artifact_reference="model.json",
        evaluation_status="unevaluated",
    )

    with pytest.raises(ValueError, match="referenced model version"):
        persist_prediction(
            session,
            prediction_id=uuid4(),
            match_id=match_id,
            feature_set_id=feature_set_id,
            model_id=model_id,
            model_version="v1",
            feature_version="v1",
            prediction_cutoff=cutoff,
            predicted_at=cutoff,
            prediction="over_2_5",
            probabilities={"over_2_5": 0.6, "under_2_5": 0.4},
            confidence=0.6,
            status="generated",
        )

    assert session.added == []
    assert not session.flushed


def test_direct_prediction_persistence_rejects_non_finite_confidence():
    session = FakeSession()
    match_id = uuid4()
    feature_set_id = uuid4()
    model_id = uuid4()
    cutoff = datetime(2024, 5, 12, 14, 0, tzinfo=timezone.utc)
    session.lookup[(FeatureSet, feature_set_id)] = FeatureSet(
        feature_set_id=feature_set_id,
        match_id=match_id,
        prediction_cutoff=cutoff,
        feature_version="v1",
        feature_generation_version="gen-1",
        source_data_reference={"snapshot_id": "snapshot-1", "source_record_ids": []},
        schema_version="schema-1",
        feature_payload={},
    )
    session.lookup[(ModelVersion, (model_id, "v1"))] = ModelVersion(
        model_id=model_id,
        model_version="v1",
        model_type="xgboost",
        feature_version="v1",
        training_run_id=uuid4(),
        training_data_reference={},
        artifact_reference="model.json",
        evaluation_status="unevaluated",
    )

    with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
        persist_prediction(
            session,
            prediction_id=uuid4(),
            match_id=match_id,
            feature_set_id=feature_set_id,
            model_id=model_id,
            model_version="v1",
            feature_version="v1",
            prediction_cutoff=cutoff,
            predicted_at=cutoff,
            prediction="over_2_5",
            probabilities={"over_2_5": 0.6, "under_2_5": 0.4},
            confidence=float("nan"),
            status="generated",
        )

    assert session.added == []
    assert not session.flushed


@pytest.mark.parametrize(
    ("availability", "error"),
    [
        (None, "unknown availability"),
        (datetime(2024, 5, 12, 14, 1, tzinfo=timezone.utc), "unavailable at prediction_cutoff"),
    ],
)
def test_prediction_batch_rejects_unproven_or_late_source_availability(availability, error):
    session = FakeSession()
    match_id = uuid4()
    source_record_id = uuid4()
    cutoff = datetime(2024, 5, 12, 14, 0, tzinfo=timezone.utc)
    session.lookup = {
        (Match, match_id): Match(
            match_id=match_id,
            sport="football",
            league="premier-league",
            season="2324",
            home_team="Arsenal",
            away_team="Chelsea",
            event_time=cutoff,
            status="scheduled",
        ),
        (SourceRecord, source_record_id): SourceRecord(
            source_record_id=source_record_id,
            source="test-provider",
            source_id="test:1",
            source_revision=1,
            source_available_ts=availability,
            ingest_ts=datetime(2024, 5, 12, 14, 5, tzinfo=timezone.utc),
        ),
    }

    with pytest.raises(ValueError, match=error):
        persist_prediction_batch(
            session,
            match_id=match_id,
            feature_set_id=uuid4(),
            feature_version="v1.0",
            feature_generation_version="gen-4",
            source_data_reference={"snapshot_id": "snapshot-42", "source_record_ids": [str(source_record_id)]},
            schema_version="schema-1",
            feature_payload={"home_elo": 1500.0},
            model_id=uuid4(),
            model_version="v3.4.1",
            model_type="xgboost",
            training_run_id=uuid4(),
            training_data_reference={"source": "training-run-72"},
            artifact_reference="s3://models/baseline-v3.json",
            configuration_reference=None,
            evaluation_status="passed",
            prediction_id=uuid4(),
            prediction_cutoff=cutoff,
            predicted_at=datetime(2024, 5, 12, 14, 6, tzinfo=timezone.utc),
            prediction="over_2_5",
            probabilities={"over": 0.61, "under": 0.39},
            confidence=0.61,
            status="published",
        )

    assert session.added == []
    assert not session.flushed