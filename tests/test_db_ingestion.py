from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.db.ingestion import persist_linked_records
from src.db.models import Match, ResultObservation, SourceRecord
from scripts.persist_linked_matches import load_verified_event_times


class FakeSession:
    def __init__(self):
        self.added = []
        self.flushed = False

    def get(self, model, identity):
        return None

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