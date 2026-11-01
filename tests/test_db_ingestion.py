from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.db.ingestion import (
    persist_feature_set,
    persist_linked_records,
    persist_model_version,
    persist_prediction,
    persist_prediction_batch,
)
from src.db.models import FeatureSet, Match, ModelVersion, Prediction, ResultObservation, SourceRecord
from scripts.persist_linked_matches import load_linked_records, load_verified_event_times


class FakeSession:
    def __init__(self):
        self.added = []
        self.flushed = False
        self.lookup = {}

    def get(self, model, identity):
        return self.lookup.get((model, identity))

    def scalar(self, statement):
        return None

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