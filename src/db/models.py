from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    CHAR,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base
from src.source_evidence import legacy_source_id_from_reference
from src.temporal import AVAILABILITY_BASIS_VALUES, PREDICTION_MODE_VALUES


_PREDICTION_MODE_SQL_VALUES = ", ".join(repr(value) for value in PREDICTION_MODE_VALUES)
_AVAILABILITY_BASIS_SQL_VALUES = ", ".join(repr(value) for value in AVAILABILITY_BASIS_VALUES)


class Match(Base):
    __tablename__ = "match"
    __table_args__ = (
        CheckConstraint("btrim(sport) <> ''", name="ck_match_sport_nonempty"),
        CheckConstraint("btrim(league) <> ''", name="ck_match_league_nonempty"),
        CheckConstraint("btrim(season) <> ''", name="ck_match_season_nonempty"),
        CheckConstraint("btrim(home_team) <> ''", name="ck_match_home_team_nonempty"),
        CheckConstraint("btrim(away_team) <> ''", name="ck_match_away_team_nonempty"),
        CheckConstraint("home_team <> away_team", name="ck_match_distinct_teams"),
        CheckConstraint(
            "actual_start_at IS NULL OR completed_at IS NULL OR actual_start_at <= completed_at",
            name="ck_match_actual_start_before_completion",
        ),
        CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled')",
            name="ck_match_status",
        ),
        CheckConstraint("updated_at >= created_at", name="ck_match_updated_after_created"),
        Index(
            "ix_match_competition_event_time", "sport", "league", "season", "event_time"
        ),
        Index("ix_match_event_time", "event_time"),
        Index("ix_match_status_event_time", "status", "event_time"),
    )

    match_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    sport: Mapped[str] = mapped_column(Text, nullable=False)
    league: Mapped[str] = mapped_column(Text, nullable=False)
    season: Mapped[str] = mapped_column(Text, nullable=False)
    home_team: Mapped[str] = mapped_column(Text, nullable=False)
    away_team: Mapped[str] = mapped_column(Text, nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    source_records: Mapped[list["SourceRecord"]] = relationship(back_populates="match")
    feature_sets: Mapped[list["FeatureSet"]] = relationship(back_populates="match")
    predictions: Mapped[list["Prediction"]] = relationship(back_populates="match")
    result_observations: Mapped[list["ResultObservation"]] = relationship(back_populates="match")


class SourceRecord(Base):
    __tablename__ = "source_record"
    __table_args__ = (
        UniqueConstraint(
            "source", "source_id", "source_revision", name="uq_source_record_identity_revision"
        ),
        UniqueConstraint("source_record_id", "match_id", name="uq_source_record_id_match"),
        CheckConstraint("btrim(source) <> ''", name="ck_source_record_source_nonempty"),
        CheckConstraint("btrim(source_id) <> ''", name="ck_source_record_source_id_nonempty"),
        CheckConstraint("source_revision >= 1", name="ck_source_record_revision_positive"),
        CheckConstraint(
            "source_available_ts IS NULL OR source_available_ts <= ingest_ts",
            name="ck_source_record_available_before_ingest",
        ),
        CheckConstraint(
            "first_observed_at IS NULL OR first_observed_at <= ingest_ts",
            name="ck_source_record_first_observed_before_ingest",
        ),
        CheckConstraint(
            "availability_basis IS NULL OR "
            f"availability_basis IN ({_AVAILABILITY_BASIS_SQL_VALUES})",
            name="ck_source_record_availability_basis",
        ),
        CheckConstraint(
            "availability_basis IS DISTINCT FROM 'provider_publication' "
            "OR source_available_ts IS NULL",
            name="ck_source_record_publication_not_accessibility",
        ),
        CheckConstraint(
            "availability_basis IS DISTINCT FROM 'first_observed' OR "
            "(first_observed_at IS NOT NULL AND source_available_ts IS NOT NULL "
            "AND first_observed_at = source_available_ts)",
            name="ck_source_record_first_observed_basis_consistent",
        ),
        CheckConstraint(
            "availability_basis IS DISTINCT FROM 'snapshot' OR "
            "(snapshot_available_at IS NOT NULL AND source_available_ts IS NOT NULL "
            "AND snapshot_available_at = source_available_ts)",
            name="ck_source_record_snapshot_basis_consistent",
        ),
        CheckConstraint(
            "availability_basis IS DISTINCT FROM 'verified_access' "
            "OR source_available_ts IS NOT NULL",
            name="ck_source_record_verified_access_basis_consistent",
        ),
        CheckConstraint(
            "original_filename IS NULL OR btrim(original_filename) <> ''",
            name="ck_source_record_filename_nonempty",
        ),
        CheckConstraint(
            "parser_version IS NULL OR btrim(parser_version) <> ''",
            name="ck_source_record_parser_version_nonempty",
        ),
        Index("ix_source_record_match_id", "match_id"),
        Index("ix_source_record_source_event_ts", "source", "source_event_ts"),
        Index(
            "ix_source_record_source_available_ts",
            "source_available_ts",
            postgresql_where=text("source_available_ts IS NOT NULL"),
        ),
        Index("ix_source_record_ingest_ts", "ingest_ts"),
    )

    source_record_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    _source_id: Mapped[str] = mapped_column("source_id", Text, nullable=False)
    source_revision: Mapped[int] = mapped_column(nullable=False, server_default="1")
    match_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("match.match_id", name="fk_source_record_match_id_match")
    )
    source_event_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_available_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_published_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snapshot_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    availability_basis: Mapped[str | None] = mapped_column(Text)
    source_available_ref: Mapped[str | None] = mapped_column(Text)
    ingest_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_reference: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    match: Mapped[Match | None] = relationship(back_populates="source_records")
    result_observations: Mapped[list["ResultObservation"]] = relationship(
        back_populates="source_record", foreign_keys="ResultObservation.source_record_id"
    )

    @property
    def source_id(self) -> str:
        return legacy_source_id_from_reference(self.raw_reference) or self._source_id

    @source_id.setter
    def source_id(self, value: str) -> None:
        self._source_id = value


class FeatureSet(Base):
    __tablename__ = "feature_set"
    __table_args__ = (
        CheckConstraint("btrim(feature_version) <> ''", name="ck_feature_set_version_nonempty"),
        CheckConstraint(
            "btrim(feature_generation_version) <> ''",
            name="ck_feature_set_generation_version_nonempty",
        ),
        CheckConstraint("btrim(schema_version) <> ''", name="ck_feature_set_schema_version_nonempty"),
        CheckConstraint(
            "jsonb_typeof(source_data_reference) = 'object'",
            name="ck_feature_set_source_reference_object",
        ),
        CheckConstraint(
            "jsonb_typeof(source_data_reference->'snapshot_id') = 'string'",
            name="ck_feature_set_snapshot_id_string",
        ),
        CheckConstraint(
            "btrim(source_data_reference->>'snapshot_id') <> ''",
            name="ck_feature_set_snapshot_id_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(source_data_reference->'source_record_ids') = 'array'",
            name="ck_feature_set_source_record_ids_array",
        ),
        CheckConstraint(
            "jsonb_typeof(feature_payload) = 'object'",
            name="ck_feature_set_payload_object",
        ),
        CheckConstraint(
            "prediction_mode IS NULL OR "
            f"prediction_mode IN ({_PREDICTION_MODE_SQL_VALUES})",
            name="ck_feature_set_prediction_mode",
        ),
        CheckConstraint(
            "prediction_mode IS DISTINCT FROM 'STRICT_PRE_MATCH' OR "
            "(scheduled_event_time_at_cutoff IS NOT NULL "
            "AND prediction_cutoff < scheduled_event_time_at_cutoff)",
            name="ck_feature_set_strict_cutoff_before_event",
        ),
        CheckConstraint(
            "artifact_content_hash IS NULL OR "
            "(char_length(btrim(artifact_content_hash)) = 64 "
            "AND btrim(artifact_content_hash) ~ '^[0-9a-f]{64}$')",
            name="ck_feature_set_artifact_sha256",
        ),
        Index(
            "ix_feature_set_match_cutoff",
            "match_id",
            text("prediction_cutoff DESC"),
        ),
        Index("ix_feature_set_version_schema", "feature_version", "schema_version"),
        Index("ix_feature_set_created_at", "created_at"),
    )

    feature_set_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    match_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("match.match_id", name="fk_feature_set_match_id_match"),
        nullable=False,
    )
    prediction_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    feature_version: Mapped[str] = mapped_column(Text, nullable=False)
    feature_generation_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_data_reference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    feature_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prediction_mode: Mapped[str | None] = mapped_column(Text)
    scheduled_event_time_at_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    artifact_content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    match: Mapped[Match] = relationship(back_populates="feature_sets")
    predictions: Mapped[list["Prediction"]] = relationship(back_populates="feature_set")


class ModelVersion(Base):
    __tablename__ = "model"
    __table_args__ = (
        CheckConstraint("btrim(model_version) <> ''", name="ck_model_version_nonempty"),
        CheckConstraint("btrim(model_type) <> ''", name="ck_model_type_nonempty"),
        CheckConstraint("btrim(feature_version) <> ''", name="ck_model_feature_version_nonempty"),
        CheckConstraint("btrim(artifact_reference) <> ''", name="ck_model_artifact_reference_nonempty"),
        CheckConstraint(
            "jsonb_typeof(training_data_reference) = 'object'",
            name="ck_model_training_reference_object",
        ),
        CheckConstraint(
            "configuration_reference IS NULL OR jsonb_typeof(configuration_reference) = 'object'",
            name="ck_model_configuration_reference_object",
        ),
        CheckConstraint(
            "feature_schema_version IS NULL OR btrim(feature_schema_version) <> ''",
            name="ck_model_feature_schema_version_nonempty",
        ),
        CheckConstraint(
            "artifact_content_hash IS NULL OR "
            "(char_length(btrim(artifact_content_hash)) = 64 "
            "AND btrim(artifact_content_hash) ~ '^[0-9a-f]{64}$')",
            name="ck_model_artifact_sha256",
        ),
        CheckConstraint(
            "evaluation_status IN ('unevaluated', 'passed', 'failed', 'retired')",
            name="ck_model_evaluation_status",
        ),
        Index("ix_model_family_created_at", "model_id", text("created_at DESC")),
        Index("ix_model_feature_evaluation_status", "feature_version", "evaluation_status"),
        Index("ix_model_training_run_id", "training_run_id"),
    )

    model_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    model_version: Mapped[str] = mapped_column(Text, primary_key=True)
    model_type: Mapped[str] = mapped_column(Text, nullable=False)
    feature_version: Mapped[str] = mapped_column(Text, nullable=False)
    training_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    training_data_reference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    artifact_reference: Mapped[str] = mapped_column(Text, nullable=False)
    feature_schema_version: Mapped[str | None] = mapped_column(Text)
    artifact_content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    configuration_reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    evaluation_status: Mapped[str] = mapped_column(Text, nullable=False)

    predictions: Mapped[list["Prediction"]] = relationship(back_populates="model_version_record")


class Prediction(Base):
    __tablename__ = "prediction"
    __table_args__ = (
        ForeignKeyConstraint(
            ["model_id", "model_version"],
            ["model.model_id", "model.model_version"],
            name="fk_prediction_model_version_model",
        ),
        CheckConstraint("btrim(feature_version) <> ''", name="ck_prediction_feature_version_nonempty"),
        CheckConstraint("btrim(prediction) <> ''", name="ck_prediction_value_nonempty"),
        CheckConstraint(
            "jsonb_typeof(probabilities) = 'object'",
            name="ck_prediction_probabilities_object",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_prediction_confidence_range",
        ),
        CheckConstraint(
            "status IN ('generated', 'superseded', 'invalid', 'published')",
            name="ck_prediction_status",
        ),
        CheckConstraint(
            "prediction_mode IS NULL OR "
            f"prediction_mode IN ({_PREDICTION_MODE_SQL_VALUES})",
            name="ck_prediction_prediction_mode",
        ),
        Index(
            "ix_prediction_match_predicted_at",
            "match_id",
            text("predicted_at DESC"),
        ),
        Index(
            "ix_prediction_model_version_predicted_at",
            "model_id",
            "model_version",
            text("predicted_at DESC"),
        ),
        Index("ix_prediction_cutoff", "prediction_cutoff"),
        Index("ix_prediction_status_predicted_at", "status", text("predicted_at DESC")),
    )

    prediction_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    match_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("match.match_id", name="fk_prediction_match_id_match"),
        nullable=False,
    )
    feature_set_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("feature_set.feature_set_id", name="fk_prediction_feature_set_id_feature_set"),
        nullable=False,
    )
    model_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    feature_version: Mapped[str] = mapped_column(Text, nullable=False)
    prediction_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prediction: Mapped[str] = mapped_column(Text, nullable=False)
    probabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    prediction_mode: Mapped[str | None] = mapped_column(Text)

    match: Mapped[Match] = relationship(back_populates="predictions")
    feature_set: Mapped[FeatureSet] = relationship(back_populates="predictions")
    model_version_record: Mapped[ModelVersion] = relationship(back_populates="predictions")


class ResultObservation(Base):
    __tablename__ = "result_observation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_record_id", "match_id"],
            ["source_record.source_record_id", "source_record.match_id"],
            name="fk_result_observation_source_record_same_match",
        ),
        UniqueConstraint(
            "result_observation_id", "match_id", name="uq_result_observation_id_match"
        ),
        ForeignKeyConstraint(
            ["supersedes_observation_id", "match_id"],
            ["result_observation.result_observation_id", "result_observation.match_id"],
            name="fk_result_observation_supersedes_same_match",
        ),
        CheckConstraint(
            "supersedes_observation_id IS NULL OR "
            "supersedes_observation_id <> result_observation_id",
            name="ck_result_observation_not_self_superseding",
        ),
        CheckConstraint(
            "observation_status IN ('observed', 'superseded', 'disputed', 'resolved')",
            name="ck_result_observation_status",
        ),
        CheckConstraint(
            "jsonb_typeof(result_data) = 'object'",
            name="ck_result_observation_result_data_object",
        ),
        CheckConstraint(
            "observation_status NOT IN ('superseded', 'disputed', 'resolved') "
            "OR (resolution_reason IS NOT NULL AND btrim(resolution_reason) <> '')",
            name="ck_result_observation_resolution_reason_required",
        ),
        CheckConstraint(
            "observation_status <> 'resolved' "
            "OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL "
            "AND btrim(resolved_by) <> '')",
            name="ck_result_observation_resolved_metadata_required",
        ),
        Index(
            "ix_result_observation_match_observed_at",
            "match_id",
            text("observed_at DESC"),
        ),
        Index("ix_result_observation_source_record_id", "source_record_id"),
        Index(
            "ix_result_observation_status_observed_at",
            "observation_status",
            text("observed_at DESC"),
        ),
        Index("ix_result_observation_supersedes_id", "supersedes_observation_id"),
    )

    result_observation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    match_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("match.match_id", name="fk_result_observation_match_id_match"),
        nullable=False,
    )
    source_record_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "source_record.source_record_id",
            name="fk_result_observation_source_record_id_source_record",
        ),
    )
    result_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation_status: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_observation_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "result_observation.result_observation_id",
            name="fk_result_observation_supersedes_observation",
        ),
    )
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)

    match: Mapped[Match] = relationship(back_populates="result_observations")
    source_record: Mapped[SourceRecord | None] = relationship(
        back_populates="result_observations", foreign_keys=[source_record_id]
    )
    supersedes_observation: Mapped["ResultObservation | None"] = relationship(
        remote_side=[result_observation_id],
        foreign_keys=[supersedes_observation_id],
        back_populates="superseding_observations",
    )
    superseding_observations: Mapped[list["ResultObservation"]] = relationship(
        foreign_keys=[supersedes_observation_id],
        back_populates="supersedes_observation",
    )