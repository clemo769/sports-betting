import pytest

from src.db.base import Base
from src.db.models import Match, ModelVersion, Prediction, ResultObservation, SourceRecord
from src.db.repositories import MatchRepository


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


def test_result_observation_enforces_same_match_source_reference():
    foreign_keys = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in ResultObservation.__table__.foreign_key_constraints
    }
    assert foreign_keys["fk_result_observation_source_record_same_match"] == (
        "source_record_id",
        "match_id",
    )


@pytest.mark.parametrize("limit", [0, 501, -1])
def test_match_repository_rejects_invalid_limits(limit):
    with pytest.raises(ValueError, match="limit must be between 1 and 500"):
        MatchRepository(session=None).list(limit=limit)


def test_match_repository_rejects_negative_offset():
    with pytest.raises(ValueError, match="offset must be non-negative"):
        MatchRepository(session=None).list(offset=-1)