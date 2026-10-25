"""Create immutable feature-set records.

Revision ID: 0002_feature_set
Revises: 0001_match_source
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002_feature_set"
down_revision: Union[str, Sequence[str], None] = "0001_match_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feature_set",
        sa.Column("feature_set_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "match_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("match.match_id", name="fk_feature_set_match_id_match"),
            nullable=False,
        ),
        sa.Column("prediction_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_version", sa.Text(), nullable=False),
        sa.Column("feature_generation_version", sa.Text(), nullable=False),
        sa.Column("source_data_reference", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("feature_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("btrim(feature_version) <> ''", name="ck_feature_set_version_nonempty"),
        sa.CheckConstraint(
            "btrim(feature_generation_version) <> ''",
            name="ck_feature_set_generation_version_nonempty",
        ),
        sa.CheckConstraint("btrim(schema_version) <> ''", name="ck_feature_set_schema_version_nonempty"),
        sa.CheckConstraint(
            "jsonb_typeof(source_data_reference) = 'object'",
            name="ck_feature_set_source_reference_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_data_reference->'snapshot_id') = 'string'",
            name="ck_feature_set_snapshot_id_string",
        ),
        sa.CheckConstraint(
            "btrim(source_data_reference->>'snapshot_id') <> ''",
            name="ck_feature_set_snapshot_id_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_data_reference->'source_record_ids') = 'array'",
            name="ck_feature_set_source_record_ids_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(feature_payload) = 'object'",
            name="ck_feature_set_payload_object",
        ),
    )
    op.create_index(
        "ix_feature_set_match_cutoff",
        "feature_set",
        ["match_id", sa.text("prediction_cutoff DESC")],
    )
    op.create_index(
        "ix_feature_set_version_schema",
        "feature_set",
        ["feature_version", "schema_version"],
    )
    op.create_index("ix_feature_set_created_at", "feature_set", ["created_at"])


def downgrade() -> None:
    op.drop_table("feature_set")