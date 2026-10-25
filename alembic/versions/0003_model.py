"""Create versioned model registry records.

Revision ID: 0003_model
Revises: 0002_feature_set
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003_model"
down_revision: Union[str, Sequence[str], None] = "0002_feature_set"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model",
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("model_type", sa.Text(), nullable=False),
        sa.Column("feature_version", sa.Text(), nullable=False),
        sa.Column("training_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "training_data_reference",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("artifact_reference", sa.Text(), nullable=False),
        sa.Column(
            "configuration_reference",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("evaluation_status", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("model_id", "model_version", name="pk_model"),
        sa.CheckConstraint("btrim(model_version) <> ''", name="ck_model_version_nonempty"),
        sa.CheckConstraint("btrim(model_type) <> ''", name="ck_model_type_nonempty"),
        sa.CheckConstraint("btrim(feature_version) <> ''", name="ck_model_feature_version_nonempty"),
        sa.CheckConstraint("btrim(artifact_reference) <> ''", name="ck_model_artifact_reference_nonempty"),
        sa.CheckConstraint(
            "jsonb_typeof(training_data_reference) = 'object'",
            name="ck_model_training_reference_object",
        ),
        sa.CheckConstraint(
            "configuration_reference IS NULL OR jsonb_typeof(configuration_reference) = 'object'",
            name="ck_model_configuration_reference_object",
        ),
        sa.CheckConstraint(
            "evaluation_status IN ('unevaluated', 'passed', 'failed', 'retired')",
            name="ck_model_evaluation_status",
        ),
    )
    op.create_index(
        "ix_model_family_created_at",
        "model",
        ["model_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_model_feature_evaluation_status",
        "model",
        ["feature_version", "evaluation_status"],
    )
    op.create_index("ix_model_training_run_id", "model", ["training_run_id"])


def downgrade() -> None:
    op.drop_table("model")