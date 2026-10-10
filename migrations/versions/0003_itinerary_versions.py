"""Current itinerary reference and immutable published planning versions."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "itinerary_heads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("current_id", sa.String(36), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_table(
        "itinerary_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("effect_id", sa.String(200), nullable=False, unique=True),
        sa.Column("base_id", sa.String(36), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("journal_seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("review", sa.JSON(), nullable=False),
        sa.Column("diff", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_itinerary_versions_conversation_id", "itinerary_versions", ["conversation_id"]
    )
    op.create_index("ix_itinerary_versions_run_id", "itinerary_versions", ["run_id"])


def downgrade():
    op.drop_table("itinerary_versions")
    op.drop_table("itinerary_heads")
