"""Create source-backed result observations.

Revision ID: 0005_result_observation
Revises: 0004_prediction
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_result_observation"
down_revision: Union[str, Sequence[str], None] = "0004_prediction"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "result_observation",
        sa.Column(
            "result_observation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "match_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("match.match_id", name="fk_result_observation_match_id_match"),
            nullable=False,
        ),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_status", sa.Text(), nullable=False),
        sa.Column(
            "supersedes_observation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "result_observation.result_observation_id",
                name="fk_result_observation_supersedes_observation",
            ),
            nullable=True,
        ),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_record_id"],
            ["source_record.source_record_id"],
            name="fk_result_observation_source_record_id_source_record",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id", "match_id"],
            ["source_record.source_record_id", "source_record.match_id"],
            name="fk_result_observation_source_record_same_match",
        ),
        sa.CheckConstraint(
            "supersedes_observation_id IS NULL OR "
            "supersedes_observation_id <> result_observation_id",
            name="ck_result_observation_not_self_superseding",
        ),
        sa.CheckConstraint(
            "observation_status IN ('observed', 'superseded', 'disputed', 'resolved')",
            name="ck_result_observation_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result_data) = 'object'",
            name="ck_result_observation_result_data_object",
        ),
        sa.CheckConstraint(
            "observation_status NOT IN ('superseded', 'disputed', 'resolved') "
            "OR (resolution_reason IS NOT NULL AND btrim(resolution_reason) <> '')",
            name="ck_result_observation_resolution_reason_required",
        ),
        sa.CheckConstraint(
            "observation_status <> 'resolved' "
            "OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL "
            "AND btrim(resolved_by) <> '')",
            name="ck_result_observation_resolved_metadata_required",
        ),
    )
    op.create_index(
        "ix_result_observation_match_observed_at",
        "result_observation",
        ["match_id", sa.text("observed_at DESC")],
    )
    op.create_index(
        "ix_result_observation_source_record_id",
        "result_observation",
        ["source_record_id"],
    )
    op.create_index(
        "ix_result_observation_status_observed_at",
        "result_observation",
        ["observation_status", sa.text("observed_at DESC")],
    )
    op.create_index(
        "ix_result_observation_supersedes_id",
        "result_observation",
        ["supersedes_observation_id"],
    )


def downgrade() -> None:
    op.drop_table("result_observation")