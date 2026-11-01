"""Generate XGBoost predictions and persist them with explicit provenance."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import xgboost as xgb

from src.db.ingestion import persist_prediction_batch
from src.db.models import ModelVersion
from src.db.session import session_scope


FEATURE_COLUMNS = ("home_elo", "away_elo", "home_form_last3", "away_form_last3")
MANIFEST_FIELDS = (
    "prediction_cutoff",
    "feature_generation_version",
    "schema_version",
    "snapshot_id",
    "source_record_ids",
)


def _load_manifest(path: Path, match_ids: set[str]) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object keyed by canonical match_id")

    missing = match_ids - manifest.keys()
    extra = manifest.keys() - match_ids
    if missing or extra:
        raise ValueError(
            f"manifest match IDs must exactly match feature rows; missing={sorted(missing)[:3]}, "
            f"extra={sorted(extra)[:3]}"
        )

    for match_id, metadata in manifest.items():
        if not isinstance(metadata, dict):
            raise ValueError(f"manifest entry for {match_id} must be a JSON object")
        absent = [field for field in MANIFEST_FIELDS if field not in metadata]
        if absent:
            raise ValueError(f"manifest entry for {match_id} is missing: {', '.join(absent)}")
        for field in ("feature_generation_version", "schema_version", "snapshot_id"):
            if not isinstance(metadata[field], str) or not metadata[field].strip():
                raise ValueError(f"manifest {field} for {match_id} must be a non-empty string")
        if not isinstance(metadata["source_record_ids"], list):
            raise ValueError(f"manifest source_record_ids for {match_id} must be a list")
        try:
            cutoff = datetime.fromisoformat(str(metadata["prediction_cutoff"]).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"manifest prediction_cutoff for {match_id} must be an ISO timestamp") from None
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError(f"manifest prediction_cutoff for {match_id} must include a timezone offset")
        metadata["prediction_cutoff"] = cutoff
    return manifest


def persist_predictions(
    features_path: Path,
    manifest_path: Path,
    *,
    model_id: UUID,
    model_version: str,
    decision_threshold: float,
    database_url: str | None = None,
) -> int:
    decision_threshold = float(decision_threshold)
    if not math.isfinite(decision_threshold) or not 0 <= decision_threshold <= 1:
        raise ValueError("decision_threshold must be between 0 and 1")

    features = pd.read_parquet(features_path)
    required_columns = {"match_id", *FEATURE_COLUMNS}
    absent_columns = required_columns.difference(features.columns)
    if absent_columns:
        raise ValueError(f"feature file is missing columns: {', '.join(sorted(absent_columns))}")
    if features["match_id"].isna().any():
        raise ValueError("feature rows require match_id values")
    features = features.copy()
    features["match_id"] = features["match_id"].astype(str)
    if features["match_id"].duplicated().any():
        raise ValueError("feature file contains duplicate match_id values")
    for match_id in features["match_id"]:
        try:
            UUID(match_id)
        except ValueError:
            raise ValueError(f"feature match_id {match_id!r} is not a UUID") from None

    manifest = _load_manifest(manifest_path, set(features["match_id"]))
    feature_values = features[list(FEATURE_COLUMNS)].apply(pd.to_numeric, errors="raise").fillna(0)
    if not np.isfinite(feature_values.to_numpy(dtype=float)).all():
        raise ValueError("feature values must be finite after missing values are filled with zero")

    with session_scope(database_url) as session:
        registered_model = session.get(ModelVersion, (model_id, model_version))
        if registered_model is None:
            raise ValueError(f"registered model {model_id}/{model_version} does not exist")
        if registered_model.model_type != "xgboost":
            raise ValueError(f"model type {registered_model.model_type!r} is not supported")
        artifact_path = Path(registered_model.artifact_reference)
        if not artifact_path.is_file():
            raise FileNotFoundError(f"registered model artifact not found: {artifact_path}")

        booster = xgb.Booster()
        booster.load_model(str(artifact_path))
        model_configuration = registered_model.configuration_reference or {}
        best_iteration = model_configuration.get("best_iteration")
        if best_iteration is None:
            iteration_range = (0, 0)
        elif isinstance(best_iteration, bool) or not isinstance(best_iteration, int) or best_iteration < 0:
            raise ValueError("registered model best_iteration must be a non-negative integer")
        else:
            iteration_range = (0, best_iteration + 1)
        probabilities = booster.predict(
            xgb.DMatrix(feature_values, feature_names=list(FEATURE_COLUMNS)),
            iteration_range=iteration_range,
        )
        if len(probabilities) != len(features):
            raise ValueError("model output count does not match feature row count")

        predicted_at = datetime.now(timezone.utc)
        for (_, row), probability_over in zip(features.iterrows(), probabilities):
            probability_over = float(probability_over)
            if not math.isfinite(probability_over) or not 0 <= probability_over <= 1:
                raise ValueError("model returned a probability outside [0, 1]")
            match_id = UUID(row["match_id"])
            metadata = manifest[row["match_id"]]
            predicts_over = probability_over >= decision_threshold
            source_data_reference = {
                "snapshot_id": metadata["snapshot_id"],
                "source_record_ids": metadata["source_record_ids"],
            }
            if metadata.get("content_hash") is not None:
                source_data_reference["content_hash"] = metadata["content_hash"]
            payload = {
                column: None if pd.isna(value) else float(value)
                for column, value in zip(FEATURE_COLUMNS, features.loc[row.name, list(FEATURE_COLUMNS)])
            }
            persist_prediction_batch(
                session,
                match_id=match_id,
                feature_set_id=uuid4(),
                feature_version=registered_model.feature_version,
                feature_version_model=registered_model.feature_version,
                feature_generation_version=metadata["feature_generation_version"],
                source_data_reference=source_data_reference,
                schema_version=metadata["schema_version"],
                feature_payload=payload,
                model_id=registered_model.model_id,
                model_version=registered_model.model_version,
                model_type=registered_model.model_type,
                training_run_id=registered_model.training_run_id,
                training_data_reference=registered_model.training_data_reference,
                artifact_reference=registered_model.artifact_reference,
                configuration_reference=registered_model.configuration_reference,
                evaluation_status=registered_model.evaluation_status,
                prediction_id=uuid4(),
                prediction_cutoff=metadata["prediction_cutoff"],
                predicted_at=predicted_at,
                prediction="over_2_5" if predicts_over else "under_2_5",
                probabilities={
                    "over_2_5": probability_over,
                    "under_2_5": 1.0 - probability_over,
                },
                confidence=probability_over if predicts_over else 1.0 - probability_over,
                status="generated",
            )
    return len(features)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--model-id", required=True, type=UUID)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--decision-threshold", required=True, type=float)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    try:
        count = persist_predictions(
            args.features,
            args.manifest,
            model_id=args.model_id,
            model_version=args.model_version,
            decision_threshold=args.decision_threshold,
            database_url=args.database_url,
        )
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))
    print(f"predictions_persisted: {count}")


if __name__ == "__main__":
    main()