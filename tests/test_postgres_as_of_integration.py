"""PostgreSQL integration coverage for M1-C as-of selection and feature generation.

These tests require TEST_DATABASE_URL to point only at sports_predictions_test.
Each test uses unique UUIDs and removes only rows it inserted.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from uuid import uuid4

import pandas as pd
import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from src.db.as_of import AsOfSelectionRepository, AsOfSelectionService
from src.db.models import Match, ModelVersion, Prediction, FeatureSet, ResultObservation, SourceRecord
from src.db.session import create_test_session_factory
from src.api.schemas import SourceRecordResponse
from src.features.feature_builder import build_as_of_features_from_database
from src.normalization.team_aliases import team_identity_evidence
from src.source_evidence import build_source_row_evidence
from src.temporal import PredictionMode


TEST_DATABASE_NAME = "sports_predictions_test"
EXPECTED_MIGRATION_HEAD = "0006_temporal_contract"


def _assert_test_database(session: Session) -> None:
    database_name = session.execute(text("SELECT current_database()")).scalar_one()
    if database_name != TEST_DATABASE_NAME:
        raise AssertionError(
            f"Refusing PostgreSQL test operation on unexpected database {database_name!r}"
        )


def _make_match(
    match_id,
    *,
    league,
    season,
    event_time,
    home_team,
    away_team,
    status,
    completed_at=None,
):
    return Match(
        match_id=match_id,
        sport="football",
        league=league,
        season=season,
        home_team=home_team,
        away_team=away_team,
        event_time=event_time,
        actual_start_at=completed_at - timedelta(hours=2) if completed_at else None,
        completed_at=completed_at,
        status=status,
    )


def _make_source(
    source_record_id,
    match_id,
    *,
    source,
    source_id,
    available_at,
    basis,
    ingest_at,
    raw_reference=None,
    source_available_ref=None,
    source_published_at=None,
    first_observed_at=None,
    snapshot_available_at=None,
):
    return SourceRecord(
        source_record_id=source_record_id,
        source=source,
        source_id=source_id,
        source_revision=1,
        match_id=match_id,
        source_event_ts=None,
        source_available_ts=available_at,
        source_published_ts=source_published_at,
        first_observed_at=first_observed_at,
        snapshot_available_at=snapshot_available_at,
        availability_basis=basis,
        source_available_ref=source_available_ref,
        ingest_ts=ingest_at,
        original_filename=f"{source_id}.json",
        parser_version="m1c3-integration",
        raw_reference=raw_reference,
    )


def _make_observation(
    observation_id,
    match_id,
    *,
    source_record_id,
    observed_at,
    home_goals,
    away_goals,
    supersedes=None,
    status="observed",
):
    resolved = status == "resolved"
    return ResultObservation(
        result_observation_id=observation_id,
        match_id=match_id,
        source_record_id=source_record_id,
        result_data={"home_goals": home_goals, "away_goals": away_goals},
        observed_at=observed_at,
        observation_status=status,
        supersedes_observation_id=supersedes,
        resolution_reason="M1-C.3 integration correction" if resolved else None,
        resolved_at=observed_at if resolved else None,
        resolved_by="integration-test" if resolved else None,
    )


@pytest.fixture(scope="session")
def postgres_test_engine():
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is not configured; PostgreSQL integration tests skipped")
    try:
        factory = create_test_session_factory()
    except RuntimeError:
        pytest.skip("TEST_DATABASE_URL is not configured; PostgreSQL integration tests skipped")

    engine = factory.kw["bind"]
    if (
        engine.url.database != TEST_DATABASE_NAME
        or engine.url.host not in {"localhost", "127.0.0.1", "::1"}
        or engine.url.port != 5432
    ):
        engine.dispose()
        pytest.fail("TEST_DATABASE_URL must target sports_predictions_test on localhost:5432")

    try:
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                database_name, server_version = connection.execute(
                    text("SELECT current_database(), version()")
                ).one()
                if database_name != TEST_DATABASE_NAME:
                    pytest.fail("Connected database identity is not sports_predictions_test")
                if not server_version.startswith("PostgreSQL 18"):
                    pytest.fail("PostgreSQL integration tests require PostgreSQL 18")
                revision = connection.execute(
                    text("SELECT version_num FROM public.alembic_version")
                ).scalar_one_or_none()
                if revision != EXPECTED_MIGRATION_HEAD:
                    pytest.fail(
                        f"Test database migration revision is {revision!r}; "
                        f"expected {EXPECTED_MIGRATION_HEAD!r}"
                    )
    except SQLAlchemyError:
        engine.dispose()
        pytest.skip("TEST_DATABASE_URL is unreachable; PostgreSQL integration tests skipped")

    yield engine
    engine.dispose()


def _seed_rows(engine, matches, sources, observations):
    matches_by_id = {match.match_id: match for match in matches}
    for source in sources:
        match = matches_by_id.get(source.match_id)
        if match is None:
            continue
        try:
            reference = json.loads(source.raw_reference) if isinstance(source.raw_reference, str) else dict(source.raw_reference or {})
        except (TypeError, ValueError):
            reference = {}
        snapshot_id = reference.get("source_snapshot_id") or f"test-snapshot:{source.source}:{source.source_id}"
        snapshot_sha256 = reference.get("source_snapshot_sha256") or reference.get("snapshot_sha256") or "a" * 64
        source_event_time = source.source_event_ts or match.event_time
        source.source_event_ts = source_event_time
        raw_row = {
            "date": source_event_time.date().isoformat(),
            "time": source_event_time.strftime("%H:%M"),
            "home_team": match.home_team,
            "away_team": match.away_team,
        }
        evidence = build_source_row_evidence(
            source=source.source,
            legacy_source_id=source.source_id,
            snapshot_id=snapshot_id,
            snapshot_sha256=snapshot_sha256,
            source_repository="test/integration",
            source_file_path=f"{source.source_id}.json",
            filename=f"{source.source_id}.json",
            row_locator={"type": "integration-source-record", "value": str(source.source_record_id)},
            raw_row_content=raw_row,
            availability={
                "status": "unknown" if source.source_available_ts is None else "verified",
                "basis": source.availability_basis,
                "available_at": source.source_available_ts.isoformat() if source.source_available_ts else None,
            },
            logical_identity_unavailable_reason="integration fixture has no provider logical identity",
            adapter_enrichment={
                "team_identity": {
                    "home": team_identity_evidence(match.home_team),
                    "away": team_identity_evidence(match.away_team),
                }
            },
        )
        reference.update({
            "source_snapshot_id": snapshot_id,
            "source_snapshot_sha256": snapshot_sha256,
            "source_event_date_raw": raw_row["date"],
            "source_event_time_raw": raw_row["time"],
            "venue_timezone": "UTC",
            "timezone_resolution_basis": "integration fixture explicit UTC",
            "source_row_evidence": evidence,
        })
        source.raw_reference = json.dumps(reference, sort_keys=True)

    with Session(engine) as session:
        with session.begin():
            _assert_test_database(session)
            session.add_all(matches)
            session.flush()
            session.add_all(sources)
            session.flush()
            for observation in observations:
                session.add(observation)
                session.flush()


def _cleanup_rows(engine, match_ids, source_ids, observation_ids):
    with Session(engine) as session:
        with session.begin():
            _assert_test_database(session)
            if observation_ids:
                session.execute(delete(ResultObservation).where(
                    ResultObservation.result_observation_id.in_(observation_ids)
                ))
            if source_ids:
                session.execute(delete(SourceRecord).where(
                    SourceRecord.source_record_id.in_(source_ids)
                ))
            if match_ids:
                session.execute(delete(Match).where(Match.match_id.in_(match_ids)))


def test_postgres_repository_reconstructs_cutoff_state_and_preserves_evidence(postgres_test_engine):
    run_id = uuid4().hex
    target_id = uuid4()
    history_id = uuid4()
    other_history_id = uuid4()
    target_time = datetime(2025, 12, 1, 12, 0, tzinfo=timezone.utc)
    early_cutoff = datetime(2025, 12, 1, 10, 0, tzinfo=timezone.utc)
    later_cutoff = datetime(2025, 12, 1, 11, 45, tzinfo=timezone.utc)
    initial_available = early_cutoff - timedelta(minutes=10)
    correction_available = early_cutoff + timedelta(minutes=30)
    ingested_at = datetime.now(timezone.utc)

    target = _make_match(
        target_id,
        league=f"m1c3-target-{run_id}",
        season="test",
        event_time=target_time,
        home_team="Target Home",
        away_team="Target Away",
        status="scheduled",
    )
    history = _make_match(
        history_id,
        league="m1c3-history",
        season="test",
        event_time=datetime(2025, 11, 1, 15, 0, tzinfo=timezone.utc),
        home_team="Historical Home",
        away_team="Historical Away",
        status="completed",
        completed_at=datetime(2025, 11, 1, 17, 0, tzinfo=timezone.utc),
    )
    other_history = _make_match(
        other_history_id,
        league="m1c3-history",
        season="test",
        event_time=datetime(2025, 10, 20, 15, 0, tzinfo=timezone.utc),
        home_team="Other Home",
        away_team="Other Away",
        status="completed",
        completed_at=datetime(2025, 10, 20, 17, 0, tzinfo=timezone.utc),
    )

    first_id, snapshot_id, unknown_id, late_id, target_source_id = (uuid4() for _ in range(5))
    first_source = _make_source(
        first_id, history_id, source="first-seen", source_id=f"first:{run_id}",
        available_at=initial_available, basis="first_observed", first_observed_at=initial_available,
        ingest_at=ingested_at, source_available_ref=f"first-observed:{run_id}",
    )
    snapshot_reference = json.dumps({
        "source_snapshot_id": f"snapshot:{run_id}",
        "source_snapshot_sha256": "a" * 64,
        "source_commit_sha": f"commit:{run_id}",
    })
    snapshot_source = _make_source(
        snapshot_id, history_id, source="snapshot", source_id=f"snapshot:{run_id}",
        available_at=initial_available, basis="snapshot", snapshot_available_at=initial_available,
        ingest_at=ingested_at, source_available_ref=f"commit:{run_id}",
        raw_reference=snapshot_reference,
    )
    unknown_source = _make_source(
        unknown_id, history_id, source="unknown", source_id=f"unknown:{run_id}",
        available_at=None, basis=None, ingest_at=ingested_at,
    )
    late_source = _make_source(
        late_id, history_id, source="late", source_id=f"late:{run_id}",
        available_at=correction_available, basis="verified_access", ingest_at=ingested_at,
        source_available_ref=f"access:{run_id}",
    )
    target_source = _make_source(
        target_source_id, target_id, source="target", source_id=f"target:{run_id}",
        available_at=early_cutoff, basis="verified_access", ingest_at=ingested_at,
        source_available_ref=f"target-access:{run_id}",
    )

    first_obs_id, correction_obs_id, target_obs_id = uuid4(), uuid4(), uuid4()
    first_observation = _make_observation(
        first_obs_id, history_id, source_record_id=first_id,
        observed_at=initial_available, home_goals=1, away_goals=0,
    )
    correction_observation = _make_observation(
        correction_obs_id, history_id, source_record_id=late_id,
        observed_at=correction_available, home_goals=0, away_goals=2,
        supersedes=first_obs_id, status="resolved",
    )
    target_observation = _make_observation(
        target_obs_id, target_id, source_record_id=target_source_id,
        observed_at=early_cutoff, home_goals=5, away_goals=0,
    )

    match_ids = [target_id, history_id, other_history_id]
    source_ids = [first_id, snapshot_id, unknown_id, late_id, target_source_id]
    observation_ids = [first_obs_id, correction_obs_id, target_obs_id]
    _seed_rows(
        postgres_test_engine,
        [target, history, other_history],
        [first_source, snapshot_source, unknown_source, late_source, target_source],
        [first_observation, correction_observation, target_observation],
    )
    try:
        with Session(postgres_test_engine) as session:
            _assert_test_database(session)
            repository = AsOfSelectionRepository(session)
            service = AsOfSelectionService(repository)
            early = service.select(
                target_id,
                prediction_cutoff=early_cutoff,
                prediction_mode="STRICT_PRE_MATCH",
            )
            repeated = service.select(
                target_id,
                prediction_cutoff=early_cutoff,
                prediction_mode="STRICT_PRE_MATCH",
            )
            later = service.select(
                target_id,
                prediction_cutoff=later_cutoff,
                prediction_mode="STRICT_PRE_MATCH",
            )

        selected_source_ids = {row.source_record_id for row in early.selected_source_records}
        assert selected_source_ids == {first_id, snapshot_id}
        first_selected = next(row for row in early.selected_source_records if row.source_record_id == first_id)
        assert first_selected.source_available_at.utcoffset() == timedelta(0)
        snapshot_selected = next(row for row in early.selected_source_records if row.source_record_id == snapshot_id)
        assert snapshot_selected.source_snapshot_id == f"snapshot:{run_id}"
        assert snapshot_selected.source_snapshot_sha256 == "a" * 64
        assert snapshot_selected.source_available_ref == f"commit:{run_id}"
        assert early.selected_result_observations[0].result_observation_id == first_obs_id
        assert correction_obs_id in {row.record_id for row in early.excluded_result_observations}
        assert target_obs_id in {row.record_id for row in early.excluded_result_observations}
        assert unknown_id in {row.record_id for row in early.excluded_source_records}
        assert late_id in {row.record_id for row in early.excluded_source_records}
        assert [row.source_record_id for row in early.selected_source_records] == [
            row.source_record_id for row in repeated.selected_source_records
        ]
        assert later.selected_result_observations[0].result_observation_id == correction_obs_id
        assert first_obs_id in {row.record_id for row in later.excluded_result_observations}
        assert early.selected_result_observations[0].observed_at.utcoffset() == timedelta(0)
        assert early.selected_result_observations[0].availability_status == "verified"

        retrospective = service.select(
            target_id,
            prediction_cutoff=early_cutoff,
            prediction_mode="RETROSPECTIVE_RESEARCH",
        )
        assert unknown_id in {row.source_record_id for row in retrospective.selected_source_records}
        assert next(
            row for row in retrospective.selected_source_records if row.source_record_id == unknown_id
        ).availability_status == "unknown"
    finally:
        _cleanup_rows(postgres_test_engine, match_ids, source_ids, observation_ids)


def test_postgres_snapshot_identity_is_distinct_from_legacy_source_id(postgres_test_engine):
    run_id = uuid4().hex
    match_id = uuid4()
    target_id = uuid4()
    cutoff = datetime(2025, 12, 1, 20, 0, tzinfo=timezone.utc)
    ingest_at = datetime.now(timezone.utc)
    match = _make_match(
        match_id,
        league=f"m1c62-identity-{run_id}",
        season="test",
        event_time=datetime(2025, 12, 1, 12, 0, tzinfo=timezone.utc),
        home_team="Identity Home",
        away_team="Identity Away",
        status="completed",
        completed_at=datetime(2025, 12, 1, 14, 0, tzinfo=timezone.utc),
    )
    target = _make_match(
        target_id,
        league=f"m1c62-target-{run_id}",
        season="test",
        event_time=datetime(2025, 12, 2, 12, 0, tzinfo=timezone.utc),
        home_team="Target Home",
        away_team="Target Away",
        status="scheduled",
    )
    sources = []
    observations = []
    for index in range(2):
        source_record_id = uuid4()
        snapshot_id = f"snapshot:{run_id}:{index}"
        snapshot_sha256 = f"{index + 1:x}" * 64
        evidence = build_source_row_evidence(
            source="provider-evidence-test",
            legacy_source_id="season.json:0",
            snapshot_id=snapshot_id,
            snapshot_sha256=snapshot_sha256,
            source_repository="test/provider",
            source_file_path="season.json",
            filename="season.json",
            row_locator={"type": "array_index", "value": 0},
            raw_row_content={"home": "Identity Home", "away": "Identity Away", "score": index},
            availability={"status": "verified", "basis": "verified_access"},
            logical_identity_unavailable_reason="fixture provides no provider logical ID",
        )
        raw_reference = json.dumps({
            "source_snapshot_id": snapshot_id,
            "source_snapshot_sha256": snapshot_sha256,
            "source_row_evidence": evidence,
        }, sort_keys=True)
        source = _make_source(
            source_record_id,
            match_id,
            source="provider-evidence-test",
            source_id="season.json:0",
            available_at=cutoff,
            basis="verified_access",
            ingest_at=ingest_at,
            source_available_ref=f"access:{index}",
            raw_reference=raw_reference,
        )
        source._source_id = evidence["identity"]["snapshot_scoped_row_id"]
        sources.append(source)
        observations.append(_make_observation(
            uuid4(),
            match_id,
            source_record_id=source_record_id,
            observed_at=cutoff,
            home_goals=1,
            away_goals=0,
        ))

    target_source_id = uuid4()
    target_source = _make_source(
        target_source_id,
        target_id,
        source="target-metadata-test",
        source_id=f"target:{run_id}",
        available_at=cutoff,
        basis="verified_access",
        ingest_at=ingest_at,
        source_available_ref=f"target-access:{run_id}",
    )
    sources.append(target_source)

    match_ids = [match_id, target_id]
    source_ids = [source.source_record_id for source in sources]
    historical_source_ids = [source.source_record_id for source in sources[:2]]
    stored_source_ids = {source._source_id for source in sources}
    observation_ids = [observation.result_observation_id for observation in observations]
    _seed_rows(postgres_test_engine, [match, target], sources, observations)
    try:
        with Session(postgres_test_engine) as session:
            _assert_test_database(session)
            stored = [session.get(SourceRecord, source_id) for source_id in historical_source_ids]
            assert [record.source_id for record in stored] == ["season.json:0", "season.json:0"]
            assert len({record._source_id for record in stored}) == 2
            assert [
                SourceRecordResponse.model_validate(record).source_id
                for record in stored
            ] == ["season.json:0", "season.json:0"]
            sql_source_ids = session.execute(
                text("SELECT source_id FROM source_record WHERE source_record_id = ANY(:ids) ORDER BY source_id"),
                {"ids": source_ids},
            ).scalars().all()
            assert set(sql_source_ids) == stored_source_ids
            assert all(
                record.source_record.source_id == "season.json:0"
                for record in [session.get(ResultObservation, observation_id) for observation_id in observation_ids]
            )

        with Session(postgres_test_engine) as session:
            _assert_test_database(session)
            service = AsOfSelectionService(AsOfSelectionRepository(session))
            selection = service.select(
                    target_id,
                prediction_cutoff=cutoff,
                prediction_mode="STRICT_PRE_MATCH",
            )
        assert {row.source_record_id for row in selection.selected_source_records} == set(historical_source_ids)
        assert len(selection.selected_result_observations) == 2
        assert not any(issue.code == "ambiguous_source_revision" for issue in selection.unresolved_issues)
    finally:
        _cleanup_rows(postgres_test_engine, match_ids, source_ids, observation_ids)


def test_postgres_feature_builder_uses_database_asof_history(postgres_test_engine, tmp_path):
    run_id = uuid4().hex
    target_id = uuid4()
    target_time = datetime(2025, 12, 1, 12, 0, tzinfo=timezone.utc)
    cutoff = datetime(2025, 12, 1, 10, 0, tzinfo=timezone.utc)
    ingested_at = datetime.now(timezone.utc)
    target_league = f"m1c3-feature-{run_id}"
    target = _make_match(
        target_id,
        league=target_league,
        season="test",
        event_time=target_time,
        home_team="Team A",
        away_team="Team Z",
        status="scheduled",
    )
    historical_matches = [
        _make_match(uuid4(), league="m1c3-history", season="test",
                    event_time=datetime(2025, 10, 1, 15, 0, tzinfo=timezone.utc),
                    home_team="Team A", away_team="Team B", status="completed",
                    completed_at=datetime(2025, 10, 1, 17, 0, tzinfo=timezone.utc)),
        _make_match(uuid4(), league="m1c3-history", season="test",
                    event_time=datetime(2025, 10, 10, 15, 0, tzinfo=timezone.utc),
                    home_team="Team C", away_team="Team A", status="completed",
                    completed_at=datetime(2025, 10, 10, 17, 0, tzinfo=timezone.utc)),
        _make_match(uuid4(), league="m1c3-history", season="test",
                    event_time=datetime(2025, 10, 20, 15, 0, tzinfo=timezone.utc),
                    home_team="Team A", away_team="Team D", status="completed",
                    completed_at=datetime(2025, 10, 20, 17, 0, tzinfo=timezone.utc)),
    ]
    sources = []
    observations = []
    target_source = _make_source(
        uuid4(),
        target_id,
        source="target-metadata",
        source_id=f"target:{run_id}",
        available_at=cutoff,
        basis="verified_access",
        ingest_at=ingested_at,
        source_available_ref=f"target-access:{run_id}",
    )
    sources.append(target_source)
    for index, (match, score) in enumerate(zip(historical_matches, [(3, 0), (0, 1), (1, 1)])):
        source_record_id = uuid4()
        snapshot_id = f"snapshot:{run_id}:{index}"
        sources.append(_make_source(
            source_record_id,
            match.match_id,
            source="test-provider",
            source_id=f"history:{run_id}:{index}",
            available_at=cutoff - timedelta(days=1),
            basis="verified_access",
            ingest_at=ingested_at,
            source_available_ref=f"access:{run_id}:{index}",
            raw_reference=json.dumps({
                "source_snapshot_id": snapshot_id,
                "source_snapshot_sha256": f"{index + 1:x}" * 64,
            }),
        ))
        observations.append(_make_observation(
            uuid4(),
            match.match_id,
            source_record_id=source_record_id,
            observed_at=cutoff - timedelta(days=1),
            home_goals=score[0],
            away_goals=score[1],
        ))

    match_ids = [target_id, *(match.match_id for match in historical_matches)]
    source_ids = [source.source_record_id for source in sources]
    observation_ids = [observation.result_observation_id for observation in observations]
    _seed_rows(postgres_test_engine, [target, *historical_matches], sources, observations)
    try:
        first = build_as_of_features_from_database(
            tmp_path / "features-a.parquet",
            prediction_cutoffs={str(target_id): cutoff},
            prediction_mode="STRICT_PRE_MATCH",
            feature_version="elo-form-v1",
            schema_version="feature-schema-v1",
            feature_generation_version="integration-test",
            database_url=os.environ["TEST_DATABASE_URL"],
            league=target_league,
            season="test",
        )
        second = build_as_of_features_from_database(
            tmp_path / "features-b.parquet",
            prediction_cutoffs={str(target_id): cutoff},
            prediction_mode="STRICT_PRE_MATCH",
            feature_version="elo-form-v1",
            schema_version="feature-schema-v1",
            feature_generation_version="integration-test",
            database_url=os.environ["TEST_DATABASE_URL"],
            league=target_league,
            season="test",
        )

        assert len(first) == 1
        row = first.iloc[0]
        assert row["match_id"] == str(target_id)
        assert row["prediction_mode"] == "STRICT_PRE_MATCH"
        assert row["prediction_cutoff"] == cutoff.isoformat()
        assert row["home_elo"] != 1500.0
        assert row["home_form_last3"] == pytest.approx(7 / 3)
        assert len(json.loads(row["source_record_ids_json"])) == 3
        assert len(json.loads(row["result_observation_ids_json"])) == 3
        assert first.to_dict(orient="records") == second.to_dict(orient="records")
        assert pd.read_parquet(tmp_path / "features-a.parquet").loc[0, "match_id"] == str(target_id)
    finally:
        _cleanup_rows(postgres_test_engine, match_ids, source_ids, observation_ids)


def test_postgres_enforces_same_match_supersession_fk(postgres_test_engine):
    match_a_id, match_b_id = uuid4(), uuid4()
    first_observation_id, invalid_observation_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    match_a = _make_match(
        match_a_id,
        league="m1c3-fk",
        season="test",
        event_time=now - timedelta(days=2),
        home_team="A",
        away_team="B",
        status="completed",
    )
    match_b = _make_match(
        match_b_id,
        league="m1c3-fk",
        season="test",
        event_time=now - timedelta(days=1),
        home_team="C",
        away_team="D",
        status="completed",
    )

    with Session(postgres_test_engine) as session:
        outer_transaction = session.begin()
        try:
            _assert_test_database(session)
            session.add_all([match_a, match_b])
            session.flush()
            first = _make_observation(
                first_observation_id,
                match_a_id,
                source_record_id=None,
                observed_at=now,
                home_goals=1,
                away_goals=0,
            )
            session.add(first)
            session.flush()
            invalid = _make_observation(
                invalid_observation_id,
                match_b_id,
                source_record_id=None,
                observed_at=now,
                home_goals=2,
                away_goals=0,
                supersedes=first_observation_id,
                status="resolved",
            )
            savepoint = session.begin_nested()
            try:
                session.add(invalid)
                session.flush()
            except IntegrityError:
                savepoint.rollback()
            else:
                savepoint.rollback()
                pytest.fail("PostgreSQL accepted a cross-match supersession reference")
        finally:
            if outer_transaction.is_active:
                outer_transaction.rollback()
