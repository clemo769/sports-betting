import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import CHAR, DateTime, Text

from src.db.base import Base
from src.db.models import (
    FeatureSet,
    Match,
    ModelVersion,
    Prediction,
    ResultObservation,
    SourceRecord,
)
from src.db.repositories import MatchRepository
from src.db.session import create_session_factory, create_test_session_factory


def test_orm_metadata_covers_all_migrated_domain_tables():
    assert set(Base.metadata.tables) == {
        "match",
        "source_record",
        "feature_set",
        "model",
        "prediction",
        "result_observation",
    }


def test_model_uses_composite_identity():
    assert [column.name for column in ModelVersion.__table__.primary_key.columns] == [
        "model_id",
        "model_version",
    ]


def test_source_record_keeps_database_column_and_public_source_id_mapping():
    assert SourceRecord.__table__.c.source_id.name == "source_id"
    assert SourceRecord._source_id.property.columns[0].name == "source_id"


def test_test_session_factory_uses_test_database_url_independently(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://app:app-password@localhost:5432/sports_predictions_dev",
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg2://app:test-password@localhost:5432/sports_predictions_test",
    )

    test_factory = create_test_session_factory()
    app_factory = create_session_factory()
    try:
        assert test_factory.kw["bind"].url.database == "sports_predictions_test"
        assert app_factory.kw["bind"].url.database == "sports_predictions_dev"
    finally:
        test_factory.kw["bind"].dispose()
        app_factory.kw["bind"].dispose()


def test_test_session_factory_requires_test_database_url(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL must be set"):
        create_test_session_factory()


def test_result_observation_enforces_same_match_source_reference():
    foreign_keys = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in ResultObservation.__table__.foreign_key_constraints
    }
    assert foreign_keys["fk_result_observation_source_record_same_match"] == (
        "source_record_id",
        "match_id",
    )


def test_temporal_m1_columns_are_additive_and_nullable_for_legacy_rows():
    nullable_columns = {
        Match: ("actual_start_at", "completed_at"),
        SourceRecord: (
            "source_published_ts",
            "first_observed_at",
            "snapshot_available_at",
            "availability_basis",
        ),
        FeatureSet: (
            "prediction_mode",
            "scheduled_event_time_at_cutoff",
            "artifact_content_hash",
        ),
        ModelVersion: ("feature_schema_version", "artifact_content_hash"),
        Prediction: ("prediction_mode",),
    }
    for model, column_names in nullable_columns.items():
        for column_name in column_names:
            assert model.__table__.c[column_name].nullable, (model.__tablename__, column_name)

    assert isinstance(Match.__table__.c.actual_start_at.type, DateTime)
    assert Match.__table__.c.actual_start_at.type.timezone
    assert isinstance(SourceRecord.__table__.c.availability_basis.type, Text)
    assert isinstance(FeatureSet.__table__.c.artifact_content_hash.type, CHAR)
    assert FeatureSet.__table__.c.artifact_content_hash.type.length == 64
    assert isinstance(ModelVersion.__table__.c.artifact_content_hash.type, CHAR)
    assert ModelVersion.__table__.c.artifact_content_hash.type.length == 64


def test_result_observation_supersession_is_same_match():
    foreign_keys = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in ResultObservation.__table__.foreign_key_constraints
    }
    assert foreign_keys["fk_result_observation_supersedes_same_match"] == (
        "supersedes_observation_id",
        "match_id",
    )


def _load_temporal_migration():
    migration_path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "0006_temporal_contract.py"
    )
    spec = importlib.util.spec_from_file_location("m1_temporal_migration", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_temporal_migration_upgrade_and_downgrade_are_additive(monkeypatch):
    migration = _load_temporal_migration()

    class OperationRecorder:
        def __init__(self):
            self.calls = []

        def __getattr__(self, operation):
            def record(*args, **kwargs):
                self.calls.append((operation, args, kwargs))

            return record

    upgrade = OperationRecorder()
    monkeypatch.setattr(migration, "op", upgrade)
    migration.upgrade()

    assert migration.revision == "0006_temporal_contract"
    assert migration.down_revision == "0005_result_observation"
    assert all(operation not in {"create_table", "drop_table"} for operation, _, _ in upgrade.calls)
    added_columns = {
        (args[0], args[1].name): args[1]
        for operation, args, _ in upgrade.calls
        if operation == "add_column"
    }
    assert len(added_columns) == 12
    assert all(column.nullable for column in added_columns.values())
    assert ("match", "actual_start_at") in added_columns
    assert ("source_record", "availability_basis") in added_columns
    assert ("feature_set", "prediction_mode") in added_columns
    assert ("model", "artifact_content_hash") in added_columns
    assert ("prediction", "prediction_mode") in added_columns
    assert any(
        operation == "create_foreign_key"
        and args[0] == "fk_result_observation_supersedes_same_match"
        for operation, args, _ in upgrade.calls
    )

    downgrade = OperationRecorder()
    monkeypatch.setattr(migration, "op", downgrade)
    migration.downgrade()
    assert all(operation not in {"create_table", "drop_table"} for operation, _, _ in downgrade.calls)
    dropped_columns = {
        (args[0], args[1])
        for operation, args, _ in downgrade.calls
        if operation == "drop_column"
    }
    assert dropped_columns == set(added_columns)
    assert any(
        operation == "drop_constraint"
        and args[0] == "fk_result_observation_supersedes_same_match"
        for operation, args, _ in downgrade.calls
    )


@pytest.mark.parametrize("limit", [0, 501, -1])
def test_match_repository_rejects_invalid_limits(limit):
    with pytest.raises(ValueError, match="limit must be between 1 and 500"):
        MatchRepository(session=None).list(limit=limit)


def test_match_repository_rejects_negative_offset():
    with pytest.raises(ValueError, match="offset must be non-negative"):
        MatchRepository(session=None).list(offset=-1)