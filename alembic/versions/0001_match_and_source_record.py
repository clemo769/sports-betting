"""Create canonical match and source record tables.

Revision ID: 0001_match_source
Revises: None
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_match_source"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "match",
        sa.Column("match_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sport", sa.Text(), nullable=False),
        sa.Column("league", sa.Text(), nullable=False),
        sa.Column("season", sa.Text(), nullable=False),
        sa.Column("home_team", sa.Text(), nullable=False),
        sa.Column("away_team", sa.Text(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("btrim(sport) <> ''", name="ck_match_sport_nonempty"),
        sa.CheckConstraint("btrim(league) <> ''", name="ck_match_league_nonempty"),
        sa.CheckConstraint("btrim(season) <> ''", name="ck_match_season_nonempty"),
        sa.CheckConstraint("btrim(home_team) <> ''", name="ck_match_home_team_nonempty"),
        sa.CheckConstraint("btrim(away_team) <> ''", name="ck_match_away_team_nonempty"),
        sa.CheckConstraint("home_team <> away_team", name="ck_match_distinct_teams"),
        sa.CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled')",
            name="ck_match_status",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="ck_match_updated_after_created"),
    )
    op.create_index(
        "ix_match_competition_event_time",
        "match",
        ["sport", "league", "season", "event_time"],
    )
    op.create_index("ix_match_event_time", "match", ["event_time"])
    op.create_index("ix_match_status_event_time", "match", ["status", "event_time"])

    op.create_table(
        "source_record",
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "match_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("match.match_id", name="fk_source_record_match_id_match"),
            nullable=True,
        ),
        sa.Column("source_event_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_available_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_available_ref", sa.Text(), nullable=True),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_reference", sa.Text(), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("parser_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "source", "source_id", "source_revision", name="uq_source_record_identity_revision"
        ),
        sa.UniqueConstraint(
            "source_record_id", "match_id", name="uq_source_record_id_match"
        ),
        sa.CheckConstraint("btrim(source) <> ''", name="ck_source_record_source_nonempty"),
        sa.CheckConstraint("btrim(source_id) <> ''", name="ck_source_record_source_id_nonempty"),
        sa.CheckConstraint("source_revision >= 1", name="ck_source_record_revision_positive"),
        sa.CheckConstraint(
            "source_available_ts IS NULL OR source_available_ts <= ingest_ts",
            name="ck_source_record_available_before_ingest",
        ),
        sa.CheckConstraint(
            "original_filename IS NULL OR btrim(original_filename) <> ''",
            name="ck_source_record_filename_nonempty",
        ),
        sa.CheckConstraint(
            "parser_version IS NULL OR btrim(parser_version) <> ''",
            name="ck_source_record_parser_version_nonempty",
        ),
    )
    op.create_index("ix_source_record_match_id", "source_record", ["match_id"])
    op.create_index(
        "ix_source_record_source_event_ts", "source_record", ["source", "source_event_ts"]
    )
    op.create_index(
        "ix_source_record_source_available_ts",
        "source_record",
        ["source_available_ts"],
        postgresql_where=sa.text("source_available_ts IS NOT NULL"),
    )
    op.create_index("ix_source_record_ingest_ts", "source_record", ["ingest_ts"])


def downgrade() -> None:
    op.drop_table("source_record")
    op.drop_table("match")