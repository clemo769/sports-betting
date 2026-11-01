from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from scripts import persist_predictions
from src.db.models import ModelVersion


def test_persist_predictions_maps_manifest_features_and_model_output(monkeypatch, tmp_path):
    match_id = uuid4()
    source_record_id = uuid4()
    artifact_path = tmp_path / "baseline.json"
    artifact_path.touch()
    model = SimpleNamespace(
        model_id=uuid4(),
        model_version="v1.0",
        model_type="xgboost",
        feature_version="features-v1",
        training_run_id=uuid4(),
        training_data_reference={"source": "test-run"},
        artifact_reference=str(artifact_path),
        configuration_reference={"objective": "binary:logistic", "best_iteration": 11},
        evaluation_status="unevaluated",
    )
    captured = {}

    class FakeSession:
        def get(self, model_type, identity):
            assert model_type is ModelVersion
            assert identity == (model.model_id, model.model_version)
            return model

    class SessionContext:
        def __enter__(self):
            return FakeSession()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class FakeBooster:
        def load_model(self, path):
            assert path == str(artifact_path)

        def predict(self, matrix, iteration_range):
            assert matrix[1] == list(persist_predictions.FEATURE_COLUMNS)
            assert iteration_range == (0, 12)
            return np.array([0.7])

    monkeypatch.setattr(persist_predictions, "session_scope", lambda database_url: SessionContext())
    monkeypatch.setattr(persist_predictions.xgb, "Booster", FakeBooster)
    monkeypatch.setattr(
        persist_predictions.xgb,
        "DMatrix",
        lambda values, feature_names: (values, feature_names),
    )
    monkeypatch.setattr(
        persist_predictions,
        "persist_prediction_batch",
        lambda session, **kwargs: captured.update(kwargs),
    )

    feature_path = tmp_path / "features.parquet"
    pd.DataFrame([{
        "match_id": str(match_id),
        "home_elo": 1510.0,
        "away_elo": 1490.0,
        "home_form_last3": np.nan,
        "away_form_last3": 1.5,
    }]).to_parquet(feature_path, index=False)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            str(match_id): {
                "prediction_cutoff": "2026-01-01T12:00:00Z",
                "feature_generation_version": "git:abc123",
                "schema_version": "feature-schema-v1",
                "snapshot_id": "snapshot-1",
                "source_record_ids": [str(source_record_id)],
            }
        }),
        encoding="utf-8",
    )

    count = persist_predictions.persist_predictions(
        feature_path,
        manifest_path,
        model_id=model.model_id,
        model_version=model.model_version,
        decision_threshold=0.8,
        database_url="postgresql://test",
    )

    assert count == 1
    assert captured["match_id"] == match_id
    assert captured["feature_version"] == "features-v1"
    assert captured["feature_generation_version"] == "git:abc123"
    assert captured["prediction_cutoff"] == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert captured["source_data_reference"] == {
        "snapshot_id": "snapshot-1",
        "source_record_ids": [str(source_record_id)],
    }
    assert captured["feature_payload"] == {
        "home_elo": 1510.0,
        "away_elo": 1490.0,
        "home_form_last3": None,
        "away_form_last3": 1.5,
    }
    assert captured["prediction"] == "under_2_5"
    assert captured["probabilities"]["over_2_5"] == 0.7
    assert captured["probabilities"]["under_2_5"] == pytest.approx(0.3)
    assert captured["confidence"] == pytest.approx(0.3)
    assert captured["status"] == "generated"
