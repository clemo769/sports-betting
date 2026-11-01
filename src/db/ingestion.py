"""Persist linker output without inventing source timestamp semantics."""
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import math
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    FeatureSet,
    Match,
    ModelVersion,
    Prediction,
    ResultObservation,
    SourceRecord,
)
from src.db.repositories import MatchRepository, SourceRecordRepository


def _require_aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value


def _aware_source_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None and value.utcoffset() is not None else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    text_value = str(value)
    return text_value if text_value.strip() else None


def _goal_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
        goals = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if goals < 0 or goals != value:
        raise ValueError(f"goal value must be a non-negative integer, got {value!r}")
    return goals


def _require_nonempty(value: Any, field_name: str) -> str:
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return str(value)


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a dictionary")
    return value


def _source_revision(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("source_revision must be a positive integer")
    try:
        revision = int(value)
        integral = float(value) == revision
    except (TypeError, ValueError, OverflowError):
        raise ValueError("source_revision must be a positive integer") from None
    if not integral or revision < 1:
        raise ValueError("source_revision must be a positive integer")
    return revision


def _validate_source_availability(
    session: Session,
    source_record_ids: list[Any],
    prediction_cutoff: datetime,
) -> None:
    for source_record_id in source_record_ids:
        if not isinstance(source_record_id, str):
            raise ValueError("source_data_reference.source_record_ids must contain UUID strings")
        try:
            source_record_uuid = UUID(source_record_id)
        except ValueError:
            raise ValueError(
                f"source_data_reference contains invalid source_record_id {source_record_id!r}"
            ) from None
        source_record = session.get(SourceRecord, source_record_uuid)
        if source_record is None:
            raise ValueError(f"source_record_id {source_record_id} does not exist")
        if source_record.source_available_ts is None:
            raise ValueError(f"source_record_id {source_record_id} has unknown availability")
        available_at = _require_aware(
            source_record.source_available_ts,
            f"source_record_id {source_record_id} source_available_ts",
        )
        if available_at > prediction_cutoff:
            raise ValueError(
                f"source_record_id {source_record_id} was unavailable at prediction_cutoff"
            )


def persist_feature_set(
    session: Session,
    *,
    feature_set_id: UUID,
    match_id: UUID,
    prediction_cutoff: datetime,
    feature_version: str,
    feature_generation_version: str,
    source_data_reference: dict[str, Any],
    schema_version: str,
    feature_payload: dict[str, Any],
) -> FeatureSet:
    """Persist one immutable feature-set instance tied to a match and cutoff."""
    prediction_cutoff = _require_aware(prediction_cutoff, "prediction_cutoff")
    feature_version = _require_nonempty(feature_version, "feature_version")
    feature_generation_version = _require_nonempty(
        feature_generation_version, "feature_generation_version"
    )
    schema_version = _require_nonempty(schema_version, "schema_version")
    source_data_reference = _require_mapping(source_data_reference, "source_data_reference")
    feature_payload = _require_mapping(feature_payload, "feature_payload")
    snapshot_id = source_data_reference.get("snapshot_id")
    source_record_ids = source_data_reference.get("source_record_ids")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("source_data_reference.snapshot_id must be a non-empty string")
    if not isinstance(source_record_ids, list):
        raise ValueError("source_data_reference.source_record_ids must be a list")
    _validate_source_availability(session, source_record_ids, prediction_cutoff)

    existing = session.get(FeatureSet, feature_set_id)
    if existing is not None:
        immutable_fields = {
            "match_id": match_id,
            "prediction_cutoff": prediction_cutoff,
            "feature_version": feature_version,
            "feature_generation_version": feature_generation_version,
            "source_data_reference": source_data_reference,
            "schema_version": schema_version,
            "feature_payload": feature_payload,
        }
        for field_name, expected_value in immutable_fields.items():
            if getattr(existing, field_name) != expected_value:
                raise ValueError(
                    f"feature set {feature_set_id} is immutable: "
                    f"{field_name} does not match the stored value"
                )
        return existing

    item = FeatureSet(
        feature_set_id=feature_set_id,
        match_id=match_id,
        prediction_cutoff=prediction_cutoff,
        feature_version=feature_version,
        feature_generation_version=feature_generation_version,
        source_data_reference=source_data_reference,
        schema_version=schema_version,
        feature_payload=feature_payload,
    )
    session.add(item)
    session.flush()
    return item


def persist_model_version(
    session: Session,
    *,
    model_id: UUID,
    model_version: str,
    model_type: str,
    feature_version: str,
    training_run_id: UUID,
    training_data_reference: dict[str, Any],
    artifact_reference: str,
    configuration_reference: dict[str, Any] | None = None,
    evaluation_status: str = "unevaluated",
) -> ModelVersion:
    """Persist one model version in the model family registry."""
    model_version = _require_nonempty(model_version, "model_version")
    model_type = _require_nonempty(model_type, "model_type")
    feature_version = _require_nonempty(feature_version, "feature_version")
    artifact_reference = _require_nonempty(artifact_reference, "artifact_reference")
    training_data_reference = _require_mapping(training_data_reference, "training_data_reference")
    if configuration_reference is not None:
        configuration_reference = _require_mapping(
            configuration_reference, "configuration_reference"
        )
    allowed_statuses = {"unevaluated", "passed", "failed", "retired"}
    if evaluation_status not in allowed_statuses:
        raise ValueError(
            f"evaluation_status must be one of {sorted(allowed_statuses)}, got {evaluation_status!r}"
        )

    existing = session.get(ModelVersion, (model_id, model_version))
    if existing is not None:
        immutable_fields = {
            "model_type": model_type,
            "feature_version": feature_version,
            "training_run_id": training_run_id,
            "training_data_reference": training_data_reference,
            "artifact_reference": artifact_reference,
            "configuration_reference": configuration_reference,
            "evaluation_status": evaluation_status,
        }
        for field_name, expected_value in immutable_fields.items():
            if getattr(existing, field_name) != expected_value:
                raise ValueError(
                    f"model version {model_id}/{model_version} is immutable: "
                    f"{field_name} does not match the registered value"
                )
        return existing

    item = ModelVersion(
        model_id=model_id,
        model_version=model_version,
        model_type=model_type,
        feature_version=feature_version,
        training_run_id=training_run_id,
        training_data_reference=training_data_reference,
        artifact_reference=artifact_reference,
        configuration_reference=configuration_reference,
        evaluation_status=evaluation_status,
    )
    session.add(item)
    session.flush()
    return item


def persist_prediction(
    session: Session,
    *,
    prediction_id: UUID,
    match_id: UUID,
    feature_set_id: UUID,
    model_id: UUID,
    model_version: str,
    feature_version: str,
    prediction_cutoff: datetime,
    predicted_at: datetime,
    prediction: str,
    probabilities: dict[str, Any],
    confidence: float | None,
    status: str,
) -> Prediction:
    """Persist one prediction for a match using a specific feature-set and model revision."""
    prediction_cutoff = _require_aware(prediction_cutoff, "prediction_cutoff")
    predicted_at = _require_aware(predicted_at, "predicted_at")
    model_version = _require_nonempty(model_version, "model_version")
    feature_version = _require_nonempty(feature_version, "feature_version")
    prediction_text = _require_nonempty(prediction, "prediction")
    probabilities = _require_mapping(probabilities, "probabilities")

    feature_set = session.get(FeatureSet, feature_set_id)
    if feature_set is None:
        raise ValueError(f"feature_set_id {feature_set_id} does not exist")
    if feature_set.match_id != match_id:
        raise ValueError("feature_set_id must belong to the same match as the prediction")
    if feature_set.feature_version != feature_version:
        raise ValueError("prediction feature_version must match the referenced feature_set")
    if feature_set.prediction_cutoff != prediction_cutoff:
        raise ValueError("prediction_cutoff must match the referenced feature_set")

    model_record = session.get(ModelVersion, (model_id, model_version))
    if model_record is None:
        raise ValueError(f"model version {model_id}/{model_version} does not exist")
    if model_record.feature_version != feature_version:
        raise ValueError("prediction feature_version must match the referenced model version")

    allowed_statuses = {"generated", "superseded", "invalid", "published"}
    if status not in allowed_statuses:
        raise ValueError(f"status must be one of {sorted(allowed_statuses)}, got {status!r}")
    if confidence is not None:
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            raise ValueError("confidence must be numeric or None") from None
        if not math.isfinite(confidence_value) or confidence_value < 0 or confidence_value > 1:
            raise ValueError("confidence must be between 0 and 1")
    else:
        confidence_value = None

    existing = session.get(Prediction, prediction_id)
    if existing is not None:
        immutable_fields = {
            "match_id": match_id,
            "feature_set_id": feature_set_id,
            "model_id": model_id,
            "model_version": model_version,
            "feature_version": feature_version,
            "prediction_cutoff": prediction_cutoff,
            "predicted_at": predicted_at,
            "prediction": prediction_text,
            "probabilities": probabilities,
            "confidence": confidence_value,
            "status": status,
        }
        for field_name, expected_value in immutable_fields.items():
            if getattr(existing, field_name) != expected_value:
                raise ValueError(
                    f"prediction {prediction_id} is immutable: "
                    f"{field_name} does not match the stored value"
                )
        return existing

    item = Prediction(
        prediction_id=prediction_id,
        match_id=match_id,
        feature_set_id=feature_set_id,
        model_id=model_id,
        model_version=model_version,
        feature_version=feature_version,
        prediction_cutoff=prediction_cutoff,
        predicted_at=predicted_at,
        prediction=prediction_text,
        probabilities=probabilities,
        confidence=confidence_value,
        status=status,
    )
    session.add(item)
    session.flush()
    return item


def persist_prediction_batch(
    session: Session,
    *,
    match_id: UUID,
    feature_set_id: UUID,
    feature_version: str,
    feature_version_model: str | None = None,
    feature_generation_version: str,
    source_data_reference: dict[str, Any],
    schema_version: str,
    feature_payload: dict[str, Any],
    model_id: UUID,
    model_version: str,
    model_type: str,
    training_run_id: UUID,
    training_data_reference: dict[str, Any],
    artifact_reference: str,
    configuration_reference: dict[str, Any] | None,
    evaluation_status: str,
    prediction_id: UUID,
    prediction_cutoff: datetime,
    predicted_at: datetime,
    prediction: str,
    probabilities: dict[str, Any],
    confidence: float | None,
    status: str,
) -> tuple[FeatureSet, ModelVersion, Prediction]:
    """Persist a single feature-set, model-version, and prediction atomically."""
    match = session.get(Match, match_id)
    if match is None:
        raise ValueError(f"match_id {match_id} does not exist")

    prediction_cutoff = _require_aware(prediction_cutoff, "prediction_cutoff")
    feature_version = _require_nonempty(feature_version, "feature_version")

    model_feature_version = (
        _require_nonempty(feature_version_model, "feature_version_model")
        if feature_version_model is not None
        else feature_version
    )
    if feature_version != model_feature_version:
        raise ValueError(
            "feature_version mismatch between feature_set and model_version: "
            f"{feature_version} != {model_feature_version}"
        )

    model_version_record = session.get(ModelVersion, (model_id, model_version))
    if model_version_record is not None:
        registered_metadata = {
            "model_type": model_type,
            "feature_version": model_feature_version,
            "training_run_id": training_run_id,
            "training_data_reference": training_data_reference,
            "artifact_reference": artifact_reference,
            "configuration_reference": configuration_reference,
            "evaluation_status": evaluation_status,
        }
        for field_name, expected_value in registered_metadata.items():
            if getattr(model_version_record, field_name) != expected_value:
                raise ValueError(
                    f"registered model version metadata mismatch for {field_name}"
                )

    source_data_reference = _require_mapping(source_data_reference, "source_data_reference")
    source_record_ids = source_data_reference.get("source_record_ids")
    if not isinstance(source_record_ids, list):
        raise ValueError("source_data_reference.source_record_ids must be a list")

    feature_set = persist_feature_set(
        session,
        feature_set_id=feature_set_id,
        match_id=match_id,
        prediction_cutoff=prediction_cutoff,
        feature_version=feature_version,
        feature_generation_version=feature_generation_version,
        source_data_reference=source_data_reference,
        schema_version=schema_version,
        feature_payload=feature_payload,
    )
    if model_version_record is None:
        model_version_record = persist_model_version(
            session,
            model_id=model_id,
            model_version=model_version,
            model_type=model_type,
            feature_version=model_feature_version,
            training_run_id=training_run_id,
            training_data_reference=training_data_reference,
            artifact_reference=artifact_reference,
            configuration_reference=configuration_reference,
            evaluation_status=evaluation_status,
        )

    prediction_record = persist_prediction(
        session,
        prediction_id=prediction_id,
        match_id=match_id,
        feature_set_id=feature_set_id,
        model_id=model_id,
        model_version=model_version,
        feature_version=feature_version,
        prediction_cutoff=prediction_cutoff,
        predicted_at=predicted_at,
        prediction=prediction,
        probabilities=probabilities,
        confidence=confidence,
        status=status,
    )
    session.flush()
    return feature_set, model_version_record, prediction_record


def persist_result_observation(
    session: Session,
    *,
    result_observation_id: UUID,
    match_id: UUID,
    result_data: dict[str, Any],
    observed_at: datetime,
    observation_status: str,
    source_record_id: UUID | None = None,
    supersedes_observation_id: UUID | None = None,
    resolution_reason: str | None = None,
    resolved_at: datetime | None = None,
    resolved_by: str | None = None,
) -> ResultObservation:
    """Append one result observation or correction without mutating history."""
    if session.get(Match, match_id) is None:
        raise ValueError(f"match_id {match_id} does not exist")
    if session.get(ResultObservation, result_observation_id) is not None:
        raise ValueError(f"result_observation_id {result_observation_id} already exists")

    allowed_statuses = {"observed", "superseded", "disputed", "resolved"}
    if observation_status not in allowed_statuses:
        raise ValueError(
            f"observation_status must be one of {sorted(allowed_statuses)}, "
            f"got {observation_status!r}"
        )
    observed_at = _require_aware(observed_at, "observed_at")
    result_data = _require_mapping(result_data, "result_data")

    if source_record_id is not None:
        source_record = session.get(SourceRecord, source_record_id)
        if source_record is None:
            raise ValueError(f"source_record_id {source_record_id} does not exist")
        if source_record.match_id != match_id:
            raise ValueError("source_record_id must belong to the same match as the observation")

    if supersedes_observation_id is not None:
        if supersedes_observation_id == result_observation_id:
            raise ValueError("an observation cannot supersede itself")
        prior_observation = session.get(ResultObservation, supersedes_observation_id)
        if prior_observation is None:
            raise ValueError(f"supersedes_observation_id {supersedes_observation_id} does not exist")
        if prior_observation.match_id != match_id:
            raise ValueError("superseded observation must belong to the same match")

    if observation_status in {"superseded", "disputed", "resolved"}:
        resolution_reason = _require_nonempty(resolution_reason, "resolution_reason")
    if resolved_at is not None:
        resolved_at = _require_aware(resolved_at, "resolved_at")
    if observation_status == "resolved":
        resolved_at = _require_aware(resolved_at, "resolved_at")
        resolved_by = _require_nonempty(resolved_by, "resolved_by")

    item = ResultObservation(
        result_observation_id=result_observation_id,
        match_id=match_id,
        source_record_id=source_record_id,
        result_data=result_data,
        observed_at=observed_at,
        observation_status=observation_status,
        supersedes_observation_id=supersedes_observation_id,
        resolution_reason=resolution_reason,
        resolved_at=resolved_at,
        resolved_by=resolved_by,
    )
    session.add(item)
    session.flush()
    return item


def persist_linked_records(
    session: Session,
    records: Iterable[Mapping[str, Any]],
    event_times: Mapping[str | UUID, datetime],
) -> dict[str, int]:
    """Persist matches and source records in the caller's transaction.

    ``event_times`` is an explicit mapping from canonical match ID to a verified,
    timezone-aware event instant. Naive source-local dates are never promoted to
    UTC. This function does not commit; the caller owns transaction boundaries.
    """
    rows = [dict(record) for record in records]
    batch_time = datetime.now(timezone.utc)
    source_identities = [
        (
            str(row.get("source") or "").strip(),
            str(row.get("source_id") or "").strip(),
            _source_revision(row.get("source_revision", 1)),
        )
        for row in rows
    ]
    if len(source_identities) != len(set(source_identities)):
        raise ValueError(
            "input contains duplicate source and source_id identities for the same source_revision"
        )

    matched_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("match_status") == "matched":
            canonical_id = str(row.get("canonical_match_id") or "")
            if not canonical_id:
                raise ValueError("matched records require canonical_match_id")
            matched_groups[canonical_id].append(row)

    prepared_matches: dict[UUID, Match] = {}
    for canonical_id, group in matched_groups.items():
        sources = {str(row.get("source") or "").strip() for row in group}
        sources.discard("")
        if len(sources) < 2:
            raise ValueError(
                f"canonical match {canonical_id} is marked matched without two independent sources"
            )
        event_time = event_times.get(canonical_id)
        if event_time is None:
            try:
                event_time = event_times.get(UUID(canonical_id))
            except ValueError:
                pass
        if event_time is None:
            raise ValueError(f"missing verified event time for canonical match {canonical_id}")
        event_time = _require_aware(event_time, f"event_times[{canonical_id}]")
        first = group[0]
        required = ("league", "season", "home_team", "away_team")
        if any(not first.get(field) for field in required):
            raise ValueError(f"canonical match {canonical_id} is missing required match fields")
        match_id = UUID(canonical_id)
        completed = all(
            row.get("home_goals") is not None and row.get("away_goals") is not None
            for row in group
        )
        prepared_matches[match_id] = Match(
            match_id=match_id,
            sport="football",
            league=str(first["league"]),
            season=str(first["season"]),
            home_team=str(first["home_team"]),
            away_team=str(first["away_team"]),
            event_time=event_time,
            status="completed" if completed else "scheduled",
        )

    match_repository = MatchRepository(session)
    source_repository = SourceRecordRepository(session)
    inserted_matches = 0
    inserted_source_records = 0
    linked_existing_source_records = 0
    inserted_result_observations = 0

    for match_id, match in prepared_matches.items():
        existing = match_repository.get(match_id)
        if existing is None:
            session.add(match)
            inserted_matches += 1
        elif (
            existing.league != match.league
            or existing.season != match.season
            or existing.home_team.strip().lower() != match.home_team.strip().lower()
            or existing.away_team.strip().lower() != match.away_team.strip().lower()
            or existing.event_time != match.event_time
        ):
            raise ValueError(f"canonical match {match_id} conflicts with the persisted match")

    for row in rows:
        source = str(row.get("source") or "").strip()
        source_id = str(row.get("source_id") or "").strip()
        revision = _source_revision(row.get("source_revision", 1))
        if not source or not source_id:
            raise ValueError("every source record requires source and source_id")
        linked_match_id = None
        if row.get("match_status") == "matched":
            linked_match_id = UUID(str(row["canonical_match_id"]))
        event_at = _aware_source_timestamp(row.get("source_event_ts"))
        available_at = _aware_source_timestamp(row.get("source_available_ts"))
        if available_at is not None and available_at > batch_time:
            raise ValueError(f"source availability timestamp is in the future for {source}:{source_id}")
        source_fields = {
            "source_event_ts": event_at,
            "source_available_ts": available_at,
            "source_available_ref": _optional_text(row.get("source_available_ref")),
            "raw_reference": _optional_text(row.get("raw_reference")),
            "original_filename": _optional_text(row.get("original_filename")),
            "parser_version": _optional_text(row.get("parser_version")),
        }
        source_record_id = uuid5(NAMESPACE_URL, f"{source}:{source_id}:{revision}")
        existing_record = source_repository.find_identity(source, source_id, revision)
        if existing_record is not None:
            for field_name, expected_value in source_fields.items():
                if getattr(existing_record, field_name) != expected_value:
                    raise ValueError(
                        f"source record {source}:{source_id} revision {revision} is immutable; "
                        "provider updates require a new source_revision"
                    )
            if (
                existing_record.match_id is None
                and linked_match_id is not None
            ):
                existing_record.match_id = linked_match_id
                linked_existing_source_records += 1
            elif (
                existing_record.match_id is not None
                and linked_match_id is not None
                and existing_record.match_id != linked_match_id
            ):
                raise ValueError(f"source record {source}:{source_id} is linked to a different match")
            source_record_id = existing_record.source_record_id
        else:
            session.add(
                SourceRecord(
                    source_record_id=source_record_id,
                    source=source,
                    source_id=source_id,
                    source_revision=revision,
                    match_id=linked_match_id,
                    source_event_ts=event_at,
                    source_available_ts=available_at,
                    source_available_ref=source_fields["source_available_ref"],
                    ingest_ts=batch_time,
                    raw_reference=source_fields["raw_reference"],
                    original_filename=source_fields["original_filename"],
                    parser_version=source_fields["parser_version"],
                )
            )
            inserted_source_records += 1

        home_goals = _goal_value(row.get("home_goals"))
        away_goals = _goal_value(row.get("away_goals"))
        if linked_match_id is not None and home_goals is not None and away_goals is not None:
            result_data = {"home_goals": home_goals, "away_goals": away_goals}
            existing_observations = (
                observation
                for observation in session.scalars(
                    select(ResultObservation).where(
                        ResultObservation.source_record_id == source_record_id
                    )
                )
                if observation.source_record_id == source_record_id
            )
            if any(observation.result_data != result_data for observation in existing_observations):
                raise ValueError(
                    f"source record {source}:{source_id} revision {revision} has conflicting result data; "
                    "provider updates require a new source_revision"
                )
            observation_id = uuid5(
                NAMESPACE_URL,
                f"result:{source}:{source_id}:{revision}:{home_goals}:{away_goals}",
            )
            if session.get(ResultObservation, observation_id) is None:
                session.add(
                    ResultObservation(
                        result_observation_id=observation_id,
                        match_id=linked_match_id,
                        source_record_id=source_record_id,
                        result_data=result_data,
                        observed_at=batch_time,
                        observation_status="observed",
                    )
                )
                inserted_result_observations += 1

    session.flush()
    return {
        "matches_inserted": inserted_matches,
        "source_records_inserted": inserted_source_records,
        "source_records_linked": linked_existing_source_records,
        "result_observations_inserted": inserted_result_observations,
    }