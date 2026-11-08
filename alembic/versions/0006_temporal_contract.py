"""Add temporal evidence, prediction modes, and artifact integrity fields.

Revision ID: 0006_temporal_contract
Revises: 0005_result_observation
Create Date: 2026-11-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_temporal_contract"
down_revision: Union[str, Sequence[str], None] = "0005_result_observation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PREDICTION_MODES = "'STRICT_PRE_MATCH', 'RETROSPECTIVE_RESEARCH'"
AVAILABILITY_BASES = "'provider_publication', 'verified_access', 'first_observed', 'snapshot'"
SHA256_CHECK = (
    "{column} IS NULL OR (char_length(btrim({column})) = 64 "
    "AND btrim({column}) ~ '^[0-9a-f]{{64}}$')"
)


def upgrade() -> None:
    op.add_column("match", sa.Column("actual_start_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("match", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_match_actual_start_before_completion",
        "match",
        "actual_start_at IS NULL OR completed_at IS NULL OR actual_start_at <= completed_at",
    )

    op.add_column(
        "source_record",
        sa.Column("source_published_ts", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_record",
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_record",
        sa.Column("snapshot_available_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("source_record", sa.Column("availability_basis", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_source_record_first_observed_before_ingest",
        "source_record",
        "first_observed_at IS NULL OR first_observed_at <= ingest_ts",
    )
    op.create_check_constraint(
        "ck_source_record_availability_basis",
        "source_record",
        f"availability_basis IS NULL OR availability_basis IN ({AVAILABILITY_BASES})",
    )
    op.create_check_constraint(
        "ck_source_record_publication_not_accessibility",
        "source_record",
        "availability_basis IS DISTINCT FROM 'provider_publication' "
        "OR source_available_ts IS NULL",
    )
    op.create_check_constraint(
        "ck_source_record_first_observed_basis_consistent",
        "source_record",
        "availability_basis IS DISTINCT FROM 'first_observed' OR "
        "(first_observed_at IS NOT NULL AND source_available_ts IS NOT NULL "
        "AND first_observed_at = source_available_ts)",
    )
    op.create_check_constraint(
        "ck_source_record_snapshot_basis_consistent",
        "source_record",
        "availability_basis IS DISTINCT FROM 'snapshot' OR "
        "(snapshot_available_at IS NOT NULL AND source_available_ts IS NOT NULL "
        "AND snapshot_available_at = source_available_ts)",
    )
    op.create_check_constraint(
        "ck_source_record_verified_access_basis_consistent",
        "source_record",
        "availability_basis IS DISTINCT FROM 'verified_access' "
        "OR source_available_ts IS NOT NULL",
    )

    op.add_column("feature_set", sa.Column("prediction_mode", sa.Text(), nullable=True))
    op.add_column(
        "feature_set",
        sa.Column("scheduled_event_time_at_cutoff", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "feature_set",
        sa.Column("artifact_content_hash", sa.CHAR(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_feature_set_prediction_mode",
        "feature_set",
        f"prediction_mode IS NULL OR prediction_mode IN ({PREDICTION_MODES})",
    )
    op.create_check_constraint(
        "ck_feature_set_strict_cutoff_before_event",
        "feature_set",
        "prediction_mode IS DISTINCT FROM 'STRICT_PRE_MATCH' OR "
        "(scheduled_event_time_at_cutoff IS NOT NULL "
        "AND prediction_cutoff < scheduled_event_time_at_cutoff)",
    )
    op.create_check_constraint(
        "ck_feature_set_artifact_sha256",
        "feature_set",
        SHA256_CHECK.format(column="artifact_content_hash"),
    )

    op.add_column(
        "model",
        sa.Column("feature_schema_version", sa.Text(), nullable=True),
    )
    op.add_column("model", sa.Column("artifact_content_hash", sa.CHAR(length=64), nullable=True))
    op.create_check_constraint(
        "ck_model_feature_schema_version_nonempty",
        "model",
        "feature_schema_version IS NULL OR btrim(feature_schema_version) <> ''",
    )
    op.create_check_constraint(
        "ck_model_artifact_sha256",
        "model",
        SHA256_CHECK.format(column="artifact_content_hash"),
    )

    op.add_column("prediction", sa.Column("prediction_mode", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_prediction_prediction_mode",
        "prediction",
        f"prediction_mode IS NULL OR prediction_mode IN ({PREDICTION_MODES})",
    )

    op.create_unique_constraint(
        "uq_result_observation_id_match",
        "result_observation",
        ["result_observation_id", "match_id"],
    )
    op.create_foreign_key(
        "fk_result_observation_supersedes_same_match",
        "result_observation",
        "result_observation",
        ["supersedes_observation_id", "match_id"],
        ["result_observation_id", "match_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_result_observation_supersedes_same_match",
        "result_observation",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_result_observation_id_match",
        "result_observation",
        type_="unique",
    )

    op.drop_constraint("ck_prediction_prediction_mode", "prediction", type_="check")
    op.drop_column("prediction", "prediction_mode")

    op.drop_constraint("ck_model_artifact_sha256", "model", type_="check")
    op.drop_constraint("ck_model_feature_schema_version_nonempty", "model", type_="check")
    op.drop_column("model", "artifact_content_hash")
    op.drop_column("model", "feature_schema_version")

    op.drop_constraint("ck_feature_set_artifact_sha256", "feature_set", type_="check")
    op.drop_constraint("ck_feature_set_strict_cutoff_before_event", "feature_set", type_="check")
    op.drop_constraint("ck_feature_set_prediction_mode", "feature_set", type_="check")
    op.drop_column("feature_set", "artifact_content_hash")
    op.drop_column("feature_set", "scheduled_event_time_at_cutoff")
    op.drop_column("feature_set", "prediction_mode")

    op.drop_constraint(
        "ck_source_record_verified_access_basis_consistent", "source_record", type_="check"
    )
    op.drop_constraint("ck_source_record_snapshot_basis_consistent", "source_record", type_="check")
    op.drop_constraint(
        "ck_source_record_first_observed_basis_consistent", "source_record", type_="check"
    )
    op.drop_constraint(
        "ck_source_record_publication_not_accessibility", "source_record", type_="check"
    )
    op.drop_constraint("ck_source_record_availability_basis", "source_record", type_="check")
    op.drop_constraint(
        "ck_source_record_first_observed_before_ingest", "source_record", type_="check"
    )
    op.drop_column("source_record", "availability_basis")
    op.drop_column("source_record", "snapshot_available_at")
    op.drop_column("source_record", "first_observed_at")
    op.drop_column("source_record", "source_published_ts")

    op.drop_constraint("ck_match_actual_start_before_completion", "match", type_="check")
    op.drop_column("match", "completed_at")
    op.drop_column("match", "actual_start_at")