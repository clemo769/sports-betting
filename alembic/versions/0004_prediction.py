"""Create immutable prediction records.

Revision ID: 0004_prediction
Revises: 0003_model
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004_prediction"
down_revision: Union[str, Sequence[str], None] = "0003_model"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prediction",
        sa.Column("prediction_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "match_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("match.match_id", name="fk_prediction_match_id_match"),
            nullable=False,
        ),
        sa.Column(
            "feature_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("feature_set.feature_set_id", name="fk_prediction_feature_set_id_feature_set"),
            nullable=False,
        ),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("feature_version", sa.Text(), nullable=False),
        sa.Column("prediction_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prediction", sa.Text(), nullable=False),
        sa.Column("probabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_id", "model_version"],
            ["model.model_id", "model.model_version"],
            name="fk_prediction_model_version_model",
        ),
        sa.CheckConstraint("btrim(feature_version) <> ''", name="ck_prediction_feature_version_nonempty"),
        sa.CheckConstraint("btrim(prediction) <> ''", name="ck_prediction_value_nonempty"),
        sa.CheckConstraint(
            "jsonb_typeof(probabilities) = 'object'",
            name="ck_prediction_probabilities_object",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_prediction_confidence_range",
        ),
        sa.CheckConstraint(
            "status IN ('generated', 'superseded', 'invalid', 'published')",
            name="ck_prediction_status",
        ),
    )
    op.create_index(
        "ix_prediction_match_predicted_at",
        "prediction",
        ["match_id", sa.text("predicted_at DESC")],
    )
    op.create_index(
        "ix_prediction_model_version_predicted_at",
        "prediction",
        ["model_id", "model_version", sa.text("predicted_at DESC")],
    )
    op.create_index("ix_prediction_cutoff", "prediction", ["prediction_cutoff"])
    op.create_index(
        "ix_prediction_status_predicted_at",
        "prediction",
        ["status", sa.text("predicted_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("prediction")